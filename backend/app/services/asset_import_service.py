"""Import investment orders (buys and sells) from a broker CSV.

A portfolio arrives as a list of orders, not as positions: a hundred rows of
"ticker, date, quantity, price, fee". Securo already knows how to turn orders
into a position — `asset_transaction_service._recompute` does the weighted
average, the fees and the realized gain — so this module's whole job is to get
the rows out of the file and onto the right holdings.

Two things shape the design:

- **Tickers are resolved once, not per row.** Creating a market-priced holding
  needs a live quote, and a broker file with 200 rows usually covers 20 or 30
  tickers. Resolving per row would make 200 provider calls and get rate-limited
  halfway through, so the distinct tickers are looked up in one batch before
  anything is written, and the preview reports the ones that came back empty.
- **The whole file is checked before a single row lands.** A sell of more units
  than the ledger holds is refused by the ledger itself, and a file that starts
  mid-history will do exactly that. Failing on row 140 after writing 139 leaves
  a portfolio that is neither the old one nor the new one, so the run either
  applies completely or not at all.
"""
import csv
import io
import logging
import uuid
from collections import Counter
from datetime import date as date_type
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.asset_group import AssetGroup
from app.models.asset_transaction import AssetTransaction
from app.models.import_log import ImportLog
from app.providers.market_price import (
    MarketPriceProvider,
    MarketPriceRateLimitedError,
    get_market_price_provider,
)
from app.schemas.asset_import import (
    AssetImportRowError,
    AssetImportSkip,
    AssetImportWarning,
    AssetOrderImport,
)
from app.services import asset_transaction_service
from app.services.asset_group_service import ensure_group_in_workspace
from app.services.import_service import (
    DATE_FORMAT_MAP,
    _sniff_csv_dialect,
    normalize_amount,
)
from app.services.rule_engine import _strip_accents

logger = logging.getLogger(__name__)

#: What makes a holding one this importer may add orders to. `market_price` is
#: the normal case. The second is narrower than it looks: an unpriced holding
#: falls back to `manual`, and has to be matched or the next import of the same
#: ticker would build a duplicate beside it — but only the ones this importer
#: created. A hand-made manual asset that happens to carry a ticker is the
#: user's own record, and rewriting its units from a file it never came from
#: would be this module taking something that is not its.
def _is_importable_holding():
    return or_(
        Asset.valuation_method == 'market_price',
        and_(Asset.valuation_method == 'manual', Asset.source == 'import'),
    )

#: How many tickers the bulk lookup may miss before we stop double-checking
#: them one by one. Past this the provider is having a bad day, not the file.
_QUOTE_FALLBACK_LIMIT = 25

#: Securo fields a CSV column can be mapped to, and which of them a file cannot
#: do without. Mirrors the transaction importer's `CSV_MAPPABLE_FIELDS`, and
#: drives both the mapping dropdowns and the downloadable template.
ASSET_CSV_MAPPABLE_FIELDS = (
    'ticker', 'date', 'quantity', 'price', 'fee', 'kind', 'currency', 'name', 'notes', 'external_id',
    'cost_basis', 'date_sold', 'proceeds',
)
ASSET_CSV_REQUIRED_FIELDS = ('ticker', 'date', 'quantity')
#: A row also has to say what the units cost, in one of the two shapes a report
#: uses: a per-unit price, or a total cost basis this module divides down.
ASSET_CSV_REQUIRED_EITHER = ('price', 'cost_basis')

#: What `asset_transactions.quantity` and `.price` can hold (migration 082):
#: 38 digits, eighteen of them after the point. Both halves are needed at once
#: — a crypto lot report carries sixteen decimals, and a meme-coin lot runs to
#: twelve digits before the point — which is why the column is not the 28 that
#: eighteen decimals of a share count would want.
#: Rounding here rather than at the database keeps the duplicate fingerprint
#: stable: a re-import has to produce the number already stored, not the one
#: the file wrote.
_LEDGER_PLACES = Decimal('1E-18')
_LEDGER_LIMIT = Decimal(10) ** 20

#: Header names brokers actually use, matched case- and accent-insensitively
#: after normalization. A file whose headers are recognised needs no mapping
#: step at all; anything else falls through to the dropdowns.
_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    # One entry per language Securo is translated into, because a broker
    # export is written in the language of the person who downloaded it.
    # Diacritics are folded before matching, so `Preço` finds `preco`; the
    # Cyrillic and Polish entries are spelled as they actually appear.
    'ticker': (
        'ticker', 'symbol', 'code', 'isin',
        # A crypto tax tool names the column after the thing, not the ticker.
        'asset', 'coin', 'coin type', 'token',
        'simbolo', 'ativo', 'papel', 'codigo',                    # pt
        'activo', 'valor',                                        # es
        'symbole', 'titre', 'actif',                              # fr
        'wertpapier', 'kuerzel', 'kurzel', 'wkn',                 # de
        'titolo', 'strumento',                                    # it
        'walor', 'instrument',                                    # pl
        'тикер', 'символ', 'бумага',                              # ru
        'тікер', 'папір',                                         # uk
    ),
    'date': (
        # `run date` before `settlement date`: a broker that reports both
        # means the trade by the first and the cash movement by the second,
        # and it is the trade that opens the lot.
        'date', 'trade date', 'run date', 'settlement date',
        # Lot reports date the acquisition; exchange histories timestamp it.
        'date acquired', 'acquisition date', 'date opened', 'timestamp',
        'date and time',
        'data', 'data do negocio', 'data negocio',                # pt
        'fecha', 'fecha operacion',                               # es
        'date de transaction', 'date operation',                  # fr
        'datum', 'handelstag', 'buchungstag',                     # de
        'data operazione',                                        # it
        'data transakcji',                                        # pl
        'дата', 'дата сделки',                                    # ru
        'дата операції',                                          # uk
    ),
    'quantity': (
        'quantity', 'qty', 'shares', 'units', 'amount of shares',
        'amount', 'coin amount', 'amount remaining', 'units remaining',
        'quantity remaining',
        'quantidade',                                             # pt
        'cantidad', 'titulos',                                    # es
        'quantite', 'nombre', 'titres',                           # fr
        'menge', 'stueck', 'stuck', 'anzahl', 'stueckzahl',       # de
        'quantita', 'numero',                                     # it
        'ilosc', 'ilość', 'liczba', 'wolumen',                    # pl
        'количество', 'кол-во', 'объем',                          # ru
        'кількість', 'обсяг',                                     # uk
    ),
    'price': (
        'price', 'unit price', 'price per share',
        'acquisition price', 'price at acquisition', 'purchase price',
        'preco', 'preco unitario', 'valor unitario',              # pt
        'precio', 'precio unitario', 'cotizacion',                # es
        'prix', 'cours', 'prix unitaire',                         # fr
        'preis', 'kurs', 'stueckpreis',                           # de
        'prezzo', 'prezzo unitario', 'quotazione',                # it
        'cena', 'cena jednostkowa',                               # pl
        'цена', 'курс',                                           # ru
        'ціна',                                                   # uk
    ),
    'fee': (
        'fee', 'fees', 'commission', 'costs',
        'taxa', 'taxas', 'corretagem', 'custos',                  # pt
        'comision', 'comisiones', 'gastos',                       # es
        'frais', 'courtage',                                      # fr
        'gebuehr', 'gebuhr', 'gebuehren', 'provision', 'kosten',  # de
        'commissione', 'commissioni', 'spese',                    # it
        'prowizja', 'oplata', 'opłata', 'koszty',                 # pl
        'комиссия', 'сбор',                                       # ru
        'комісія', 'збір',                                        # uk
    ),
    'kind': (
        # The specific names first: a broker's `Type` column is as likely to
        # say "Cash" or "Margin" as it is to say "Buy", so anything that names
        # the transaction outright wins over it.
        'kind', 'side', 'operation', 'buy/sell', 'action',
        'transaction type', 'transaction kind', 'type',
        'tipo', 'operacao', 'c/v',                                # pt
        'operacion', 'compra/venta', 'sentido',                   # es
        'sens', 'achat/vente',                                    # fr
        'art', 'richtung', 'kauf/verkauf', 'transaktionsart',     # de
        'operazione', 'segno', 'acquisto/vendita',                # it
        'rodzaj', 'operacja', 'kupno/sprzedaz', 'strona',         # pl
        'операция', 'направление', 'покупка/продажа',             # ru
        'операція', 'купівля/продаж',                             # uk
    ),
    'currency': (
        'currency', 'ccy', 'moeda', 'moneda', 'divisa', 'devise', 'monnaie',
        'waehrung', 'wahrung', 'valuta', 'waluta', 'валюта',
    ),
    'name': (
        'name', 'description', 'security',
        'nome', 'descricao', 'nombre', 'descripcion', 'nom', 'libelle',
        'bezeichnung', 'beschreibung', 'descrizione', 'nazwa', 'opis',
        'название', 'наименование', 'назва',
    ),
    'notes': (
        'notes', 'note', 'observacao', 'observacoes', 'obs', 'observaciones',
        'remarques', 'notizen', 'bemerkung', 'notatki', 'uwagi',
        'заметки', 'примечание', 'нотатки', 'примітки',
    ),
    'external_id': (
        'external_id', 'id', 'order id', 'trade id', 'reference',
        # Deliberately not the crypto tools' "unique asset identifier": that
        # names the coin, not the row, so keying on it would collapse every
        # lot of the same token into one.
        'internal id', 'transaction id', 'txid', 'transaction hash',
    ),
    # The lot shape: a report that gives a whole lot on one line, with totals
    # rather than unit prices, and the sale on the same row as the purchase.
    'cost_basis': (
        'cost basis', 'cost basis remaining', 'total cost', 'basis', 'book cost',
        # An exchange states the fiat the units were worth rather than a
        # cost basis, which for an acquisition is the same number.
        'usd value', 'fair market value', 'native amount', 'subtotal',
    ),
    'date_sold': ('date sold', 'date disposed', 'disposal date', 'date closed', 'sold date'),
    'proceeds': ('proceeds', 'gross proceeds', 'sale proceeds'),
}

#: Values that mean "this row is a sale". Everything else is read as a buy,
#: except a negative quantity, which is the convention most brokers export.
_SELL_WORDS = {
    'sell', 'sale', 'sold', 's',
    'venda', 'vender', 'saida', 'v',                              # pt
    'venta',                                                      # es
    'vendre', 'vente',                                            # fr
    'verkauf', 'verkaufen', 'vk',                                 # de
    'vendita', 'vendere',                                         # it
    'sprzedaz', 'sprzedaż', 'sprzedac',                           # pl
    'продажа', 'продать', 'продаж', 'продати',                    # ru/uk
}
_BUY_WORDS = {
    'buy', 'purchase', 'bought', 'b', 'reinvestment', 'reinvested',
    'compra', 'comprar', 'entrada', 'c',                          # pt/es
    'achat', 'acheter',                                           # fr
    'kauf', 'kaufen', 'kf',                                       # de
    'acquisto', 'acquistare',                                     # it
    'kupno', 'zakup', 'kupic', 'kupić',                           # pl
    'покупка', 'купить', 'купівля', 'купити',                     # ru/uk
}

#: The rest of the vocabulary a crypto history uses, which a broker file never
#: needed. Three groups, because they land in three different places:
#:
#: - **Acquired.** Units arriving from somewhere that is not a purchase — a
#:   staking reward, an airdrop, a fork, a distribution from an insolvency
#:   estate. They open a Lot like a buy does, at whatever the row says they
#:   were worth, which for an airdrop is usually nothing. Zero basis is the
#:   honest answer there: the units cost nothing, so the whole disposal is gain.
#: - **Transferred.** The same person's coins moving between their own wallets.
#:   Basis travels with them, so a transfer is not an acquisition and not a
#:   disposal; importing one would invent a Lot that never existed. Skipped,
#:   with the reason said out loud rather than dropped.
#: - **Signed.** A conversion of one asset into another. Which side of it this
#:   row is depends on the sign of the quantity, exactly as for a broker file.
_ACQUIRE_WORDS = {
    'staking reward', 'staking rewards', 'staking', 'reward', 'rewards',
    'interest income', 'interest', 'income', 'mining', 'mined', 'airdrop',
    'fork', 'hard fork', 'distribution', 'insolvency distribution', 'claim',
    'dividend', 'dividend received',
    'bonus', 'gift received', 'rebate', 'cashback', 'award', 'referral',
}
_TRANSFER_WORDS = {
    'transfer', 'transfer in', 'transfer out', 'deposit', 'withdrawal',
    'withdraw', 'send', 'sent', 'receive', 'received', 'move',
}
_SIGNED_WORDS = {'trade', 'convert', 'converted', 'conversion', 'swap', 'exchange'}


def _classify_kind(word: str) -> Optional[str]:
    """What a type column's value means: buy, sell, acquire, transfer, signed.

    `None` is "a word we do not model" — reported as a skipped row rather than
    a malformed one, because the row is perfectly well-formed and the gap is
    ours.
    """
    if word in _SELL_WORDS:
        return 'sell'
    if word in _BUY_WORDS:
        return 'buy'
    if word in _ACQUIRE_WORDS:
        return 'acquire'
    if word in _TRANSFER_WORDS:
        return 'transfer'
    if word in _SIGNED_WORDS:
        return 'signed'
    # A broker's action column is a sentence, not a word: "YOU BOUGHT FIDELITY
    # ZERO TOTAL MARKET INDEX (FZROX)". One recognised word in it decides,
    # which is safe only because no vocabulary here contains the negation of
    # another — there is no phrase that is a buy because it says "sell".
    tokens = set(word.split())
    for vocabulary, meaning in (
        (_SELL_WORDS, 'sell'), (_BUY_WORDS, 'buy'),
        (_ACQUIRE_WORDS, 'acquire'), (_TRANSFER_WORDS, 'transfer'),
        (_SIGNED_WORDS, 'signed'),
    ):
        if tokens & vocabulary:
            return meaning
    return None


def _to_ledger_scale(value: Optional[Decimal]) -> Optional[Decimal]:
    """Round to the precision the ledger stores, or `None` if it will not fit.

    Doing it here and not at the database is what makes a re-import idempotent:
    the fingerprint has to be computed from the number that was written, and
    Postgres would have rounded it on the way in.
    """
    if value is None or abs(value) >= _LEDGER_LIMIT:
        return None
    with localcontext() as ctx:
        # Wider than the default 28 so quantizing twenty integer digits down
        # to eighteen decimals is arithmetic rather than an InvalidOperation.
        ctx.prec = 60
        return value.quantize(_LEDGER_PLACES, rounding=ROUND_HALF_UP)


def _decode(content: bytes) -> str:
    """Broker exports are not always UTF-8; fall back rather than blow up.

    Leading blank lines come off with the encoding: a broker that puts its
    account name, or nothing at all, above the header row would otherwise have
    that line read as the header and every column go unrecognised.
    """
    text = None
    for encoding in ('utf-8-sig', 'latin-1'):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode('utf-8', errors='replace')
    return text.lstrip('\ufeff\r\n \t')


def detect_columns(content: bytes) -> list[str]:
    """The file's header names, as written, for the mapping dropdowns."""
    text = _decode(content)
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff_csv_dialect(text))
    return [f.strip() for f in (reader.fieldnames or []) if f and f.strip()]


def _normalize_header(value: str) -> str:
    """Fold a header (or a buy/sell word) to its comparable form.

    Accents come off because a Brazilian export writes `Preço` and `Operação`,
    and a header that only differs by a diacritic is the same header.
    """
    folded = _strip_accents(value.strip().lower().replace('_', ' '))
    return ' '.join(folded.split())


def _without_parenthetical(value: str) -> str:
    """`price ($)` -> `price`, `date acquired (america/los_angeles)` -> `date acquired`.

    Reports qualify a column in brackets — the currency, the timezone, the
    unit — and the qualifier is never part of what the column *is*.
    """
    depth = 0
    kept = []
    for char in value:
        if char == '(':
            depth += 1
        elif char == ')':
            depth = max(depth - 1, 0)
        elif depth == 0:
            kept.append(char)
    return ' '.join(''.join(kept).split())


def _header_index(headers: list[str]) -> dict[str, str]:
    """Every comparable form of each header, first header wins a collision."""
    index: dict[str, str] = {}
    for header in headers:
        normalized = _normalize_header(header)
        index.setdefault(normalized, header)
        index.setdefault(_without_parenthetical(normalized), header)
    return index


def _auto_mapping(headers: list[str]) -> dict[str, str]:
    """Guess which column is which, so a recognisable file needs no mapping."""
    normalized = _header_index(headers)
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for field, candidates in _COLUMN_CANDIDATES.items():
        for candidate in candidates:
            header = normalized.get(candidate)
            # One column answers for one field: `Cost Basis` claimed as the
            # basis must not also come back as the price of a different file.
            if header is not None and header not in taken:
                mapping[field] = header
                taken.add(header)
                break
    return mapping


def _parse_date(raw: str, date_format: Optional[str]) -> Optional[date_type]:
    raw = raw.strip()
    if not raw:
        return None
    formats = []
    if date_format and date_format in DATE_FORMAT_MAP:
        formats.append(DATE_FORMAT_MAP[date_format])
    formats.extend([
        '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d', '%d.%m.%Y',
        # Written out, the way an exchange's own export dates a row.
        '%B %d, %Y %I:%M %p', '%b %d, %Y %I:%M %p', '%B %d, %Y', '%b %d, %Y',
    ])
    for fmt in formats:
        try:
            return datetime.strptime(raw[:10] if len(raw) > 10 and fmt == '%Y-%m-%d' else raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(raw: str) -> Optional[Decimal]:
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return Decimal(str(normalize_amount(raw)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_orders_csv(
    content: bytes,
    column_mapping: Optional[dict[str, str]] = None,
    date_format: Optional[str] = None,
) -> tuple[
    list[AssetOrderImport],
    list[AssetImportRowError],
    list[AssetImportSkip],
    list[str],
]:
    """Read a broker or crypto-tax CSV into orders, plus what could not be read.

    Two file shapes go through here, because they differ only in their columns:

    - **An order history** — one buy or sell per row, with a unit price. What a
      broker exports.
    - **A lot report** — one *lot* per row, with a total cost basis rather than
      a unit price, and, for a lot that has already been sold, the disposal on
      the same line. What a crypto tax tool exports once it has reconciled a
      history spanning exchanges, transfers and insolvency distributions. Such
      a row becomes two orders: the buy that opened the lot and the sell that
      closed it.

    Bad rows are reported rather than skipped in silence: a file where a third
    of the rows had an unreadable date should say so before anything is
    imported, not quietly bring in the other two thirds. Rows that are fine but
    create nothing — a transfer between the user's own wallets, a transaction
    type this module does not model — come back as skips, which is a different
    thing from an error and reads differently in the preview.
    """
    text = _decode(content)
    dialect = _sniff_csv_dialect(text)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    # Strip in place, keeping the positions: an export that writes
    # `id, Coin type, Coin amount` keys every row by the padded name, and a
    # mapping built from the trimmed one would then read every cell as empty.
    reader.fieldnames = [f.strip() if f else f for f in (reader.fieldnames or [])]
    headers = [f for f in reader.fieldnames if f]
    if not headers:
        raise ValueError('CSV has no header row')

    mapping = {k: v for k, v in (column_mapping or {}).items() if v}
    for field, header in _auto_mapping(headers).items():
        mapping.setdefault(field, header)

    missing = [f for f in ASSET_CSV_REQUIRED_FIELDS if f not in mapping]
    if not any(f in mapping for f in ASSET_CSV_REQUIRED_EITHER):
        missing.append(' or '.join(ASSET_CSV_REQUIRED_EITHER))
    if missing:
        raise ValueError(f"Missing required column mapping: {', '.join(missing)}")

    def cell(row: dict, field: str) -> str:
        header = mapping.get(field)
        if not header:
            return ''
        return (row.get(header) or '').strip()

    orders: list[AssetOrderImport] = []
    errors: list[AssetImportRowError] = []
    skips: list[AssetImportSkip] = []

    for index, row in enumerate(reader, start=2):  # row 1 is the header
        if not any((v or '').strip() for v in row.values()):
            continue

        ticker = cell(row, 'ticker').upper()
        if not ticker:
            errors.append(AssetImportRowError(row=index, reason='missing_ticker'))
            continue

        order_date = _parse_date(cell(row, 'date'), date_format)
        if order_date is None:
            errors.append(AssetImportRowError(row=index, reason='invalid_date', ticker=ticker))
            continue

        raw_quantity = _parse_decimal(cell(row, 'quantity'))
        quantity = _to_ledger_scale(raw_quantity)
        if quantity is None:
            errors.append(AssetImportRowError(row=index, reason='invalid_quantity', ticker=ticker))
            continue
        if quantity == 0:
            if raw_quantity != 0:
                # Real units, too small for the column to hold. Rounding them
                # away silently is the one thing a cost-basis import must not do.
                errors.append(AssetImportRowError(
                    row=index, reason='below_ledger_scale', ticker=ticker,
                    detail=cell(row, 'quantity'),
                ))
                continue
            # A cash dividend, a fee line, a lot the file has already exhausted:
            # readable, and it opens or closes nothing. Not a malformed row.
            skips.append(AssetImportSkip(row=index, ticker=ticker, reason='no_units'))
            continue

        # Buy or sell comes from an explicit column when the file has one, and
        # from the sign of the quantity otherwise — the convention brokers use.
        kind_word = _normalize_header(cell(row, 'kind'))
        meaning = _classify_kind(kind_word) if kind_word else 'signed'
        if meaning == 'transfer':
            skips.append(AssetImportSkip(row=index, ticker=ticker, reason='transfer'))
            continue
        if meaning is None:
            skips.append(AssetImportSkip(
                row=index, ticker=ticker, reason='unsupported_type', detail=cell(row, 'kind'),
            ))
            continue

        sold_date = _parse_date(cell(row, 'date_sold'), date_format)
        # A lot report row is an acquisition whatever its type column called
        # the disposal on the other half of the line.
        if sold_date or meaning in ('buy', 'acquire'):
            kind = 'buy'
        elif meaning == 'sell':
            kind = 'sell'
        else:
            kind = 'sell' if quantity < 0 else 'buy'

        price = _price_for(cell(row, 'price'), cell(row, 'cost_basis'), quantity)
        if price is None and meaning == 'acquire':
            # A reward or an airdrop the file put no value on cost nothing, so
            # the whole eventual disposal is gain. Zero, not unreadable.
            price = Decimal('0')
        if price is None or price < 0:
            errors.append(AssetImportRowError(row=index, reason='invalid_price', ticker=ticker))
            continue

        external_id = cell(row, 'external_id') or None

        fee = _parse_decimal(cell(row, 'fee')) or Decimal('0')

        def build(when: date_type, side: str, unit_price: Decimal) -> AssetOrderImport:
            return AssetOrderImport(
                row=index,
                ticker=ticker,
                date=when,
                kind=side,
                quantity=abs(quantity),
                price=unit_price,
                # One lot line states one fee. Charging it to the buy and again
                # to the sell would bill it twice for a single round trip.
                fee=fee if side == kind else Decimal('0'),
                currency=(cell(row, 'currency') or None),
                name=(cell(row, 'name') or None),
                notes=(cell(row, 'notes') or None),
                # One row, two orders, so the file's own id cannot key both.
                external_id=(
                    f'{external_id}:{side}' if external_id and sold_date else external_id
                ),
            )

        orders.append(build(order_date, kind, price))

        if sold_date is None:
            continue
        proceeds = _to_ledger_scale(_total_over(cell(row, 'proceeds'), quantity))
        if proceeds is None:
            errors.append(AssetImportRowError(row=index, reason='invalid_proceeds', ticker=ticker))
            continue
        orders.append(build(sold_date, 'sell', proceeds))

    return orders, errors, skips, headers


def _price_for(price_cell: str, basis_cell: str, quantity: Decimal) -> Optional[Decimal]:
    """The per-unit price, from whichever of the two shapes the file uses.

    A lot report states the *total* the lot cost rather than a unit price, so
    the divide is not a nicety: mapping that column onto `price` directly would
    multiply the cost basis by the number of units held.
    """
    unit = _parse_decimal(price_cell)
    if unit is not None:
        return _to_ledger_scale(unit)
    return _to_ledger_scale(_total_over(basis_cell, quantity))


def _total_over(total_cell: str, quantity: Decimal) -> Optional[Decimal]:
    """A stated total spread over the units it covers."""
    total = _parse_decimal(total_cell)
    return None if total is None else abs(total) / abs(quantity)



async def resolve_tickers(
    tickers: list[str],
    *,
    market_provider: Optional[MarketPriceProvider] = None,
) -> dict[str, bool]:
    """Which of these tickers the price provider recognises.

    One batch call answers for the whole file, which is what keeps a 200-row
    import from making 200 requests. The bulk endpoint is not authoritative
    though: it answers with an empty result often enough — the same ticker can
    come back priced and then empty seconds later — that treating a miss as
    proof would reject real holdings. So the few it did not answer for are
    confirmed one by one against the quote endpoint, which is.

    A ticker nobody recognises can still be imported onto a holding that
    already exists in the workspace; it only blocks a holding that would have
    to be created.
    """
    provider = market_provider or get_market_price_provider()
    unique = sorted({t.upper() for t in tickers if t})
    if not unique:
        return {}

    resolved: dict[str, bool] = {}
    try:
        prices = await provider.get_latest_prices(unique)
        resolved = {t: prices.get(t) is not None for t in unique}
    except MarketPriceRateLimitedError:
        raise  # the endpoint turns this into a 429 the user can act on
    except Exception:
        # Swallowing this silently would report every ticker as unknown and
        # blame the file for the provider's outage.
        logger.warning("Bulk price lookup failed; falling back to quotes", exc_info=True)
        resolved = {t: False for t in unique}

    unconfirmed = [t for t in unique if not resolved.get(t)]
    for ticker in unconfirmed[:_QUOTE_FALLBACK_LIMIT]:
        try:
            resolved[ticker] = await provider.get_quote(ticker) is not None
        except MarketPriceRateLimitedError:
            raise
        except Exception:
            logger.warning("Quote lookup failed for %s", ticker, exc_info=True)
            resolved[ticker] = False
    return resolved


async def _existing_holdings(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    group_id: Optional[uuid.UUID],
    tickers: list[str],
) -> dict[str, Asset]:
    if not tickers:
        return {}
    result = await session.execute(
        select(Asset).where(
            Asset.workspace_id == workspace_id,
            _is_importable_holding(),
            Asset.group_id == group_id,
            Asset.ticker.in_(sorted({t.upper() for t in tickers})),
        )
    )
    return {a.ticker.upper(): a for a in result.scalars().all() if a.ticker}


async def _holdings_in_other_wallets(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    group_id: Optional[uuid.UUID],
    tickers: list[str],
) -> dict[str, tuple[Asset, Optional[str]]]:
    """The same tickers held under a *different* wallet, with that wallet's name.

    Holdings are scoped per wallet, so importing AAPL into one wallet while
    AAPL already sits in another is a legitimate thing to do — two brokers,
    two positions. It is also exactly what a mis-picked wallet looks like, and
    the portfolio then counts the same shares twice, so the preview says it out
    loud instead of leaving it to be noticed later.
    """
    if not tickers:
        return {}
    result = await session.execute(
        select(Asset, AssetGroup.name)
        .outerjoin(AssetGroup, AssetGroup.id == Asset.group_id)
        .where(
            Asset.workspace_id == workspace_id,
            _is_importable_holding(),
            Asset.group_id.is_not(None) if group_id is None else Asset.group_id != group_id,
            Asset.ticker.in_(sorted({t.upper() for t in tickers})),
        )
    )
    return {asset.ticker.upper(): (asset, name) for asset, name in result.all() if asset.ticker}


async def _already_imported(
    session: AsyncSession,
    asset_ids: list[uuid.UUID],
) -> tuple[Counter, set[uuid.UUID]]:
    """How many ledger rows of each fingerprint these holdings already carry,
    and which of them have a ledger at all.

    Re-uploading the same file is the normal way people fix a mapping mistake,
    so a repeat run should add nothing rather than double the position. Counted
    rather than merely present, because two genuinely distinct buys of the same
    size on the same day at the same price are a real thing a crypto history
    does several times a page — under a plain set the second one could never be
    imported after the first.
    """
    if not asset_ids:
        return Counter(), set()
    result = await session.execute(
        select(AssetTransaction).where(AssetTransaction.asset_id.in_(asset_ids))
    )
    seen: Counter = Counter()
    with_ledger: set[uuid.UUID] = set()
    for tx in result.scalars().all():
        with_ledger.add(tx.asset_id)
        if tx.external_id:
            seen[('external', tx.asset_id, tx.external_id)] += 1
        seen[('row', tx.asset_id, tx.date, tx.kind, tx.quantity, tx.price)] += 1
    return seen, with_ledger


async def import_orders(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    orders: list[AssetOrderImport],
    *,
    group_id: Optional[uuid.UUID] = None,
    dry_run: bool = False,
    filename: Optional[str] = None,
    market_provider: Optional[MarketPriceProvider] = None,
    allow_unpriced: bool = False,
) -> dict:
    """Apply a file of orders to the workspace's holdings.

    Returns the counts the UI reports, and — on a dry run — the same numbers
    without writing anything, so the preview can promise what the import will
    do instead of guessing.

    `allow_unpriced` is the escape hatch for the holdings a price feed will
    never answer for: a delisted stock, or the token an insolvency estate hands
    out. Those get a manual holding with the basis the file gives and no live
    price, which is the truthful record. It is off by default because an
    unrecognised ticker is nearly always a typo.
    """
    # Before the dry-run branch too: the preview has to refuse a wallet the
    # commit will refuse, not promise an import that then fails.
    await ensure_group_in_workspace(session, group_id, workspace_id)

    ordered = sorted(orders, key=lambda o: (o.date, o.row))
    tickers = [o.ticker for o in ordered]

    holdings = await _existing_holdings(session, workspace_id, group_id, tickers)
    seen, with_ledger = await _already_imported(session, [a.id for a in holdings.values()])

    elsewhere = await _holdings_in_other_wallets(session, workspace_id, group_id, tickers)
    warnings: list[AssetImportWarning] = []
    if elsewhere:
        seen_elsewhere, _ = await _already_imported(session, [a.id for a, _ in elsewhere.values()])
        for ticker, (other, wallet_name) in sorted(elsewhere.items()):
            # Same orders already on the other holding is the strong signal:
            # this is the same position about to be counted twice.
            duplicated = any(
                seen_elsewhere[('row', other.id, o.date, o.kind, o.quantity, o.price)] > 0
                for o in ordered if o.ticker == ticker
            )
            warnings.append(AssetImportWarning(
                ticker=ticker,
                reason='orders_already_in_other_wallet' if duplicated else 'exists_in_other_wallet',
                wallet=wallet_name,
            ))

    missing_tickers = sorted({t for t in tickers if t not in holdings})
    resolvable = await resolve_tickers(missing_tickers, market_provider=market_provider) if missing_tickers else {}

    errors: list[AssetImportRowError] = []
    skips: list[AssetImportSkip] = []
    accepted: list[AssetOrderImport] = []

    # Units per ticker as the file is replayed, so a sell that would leave the
    # position negative is caught here rather than by the ledger halfway
    # through the write.
    reported: dict[str, Decimal] = {
        ticker: Decimal(str(asset.units or 0)) for ticker, asset in holdings.items()
    }
    units = dict(reported)

    for order in ordered:
        if order.ticker not in holdings and not resolvable.get(order.ticker, False):
            if not allow_unpriced:
                errors.append(AssetImportRowError(row=order.row, reason='unknown_ticker', ticker=order.ticker))
                continue

        asset = holdings.get(order.ticker)
        if asset is not None:
            fingerprint = ('row', asset.id, order.date, order.kind, order.quantity, order.price)
            external = ('external', asset.id, order.external_id) if order.external_id else None
            if (external is not None and seen[external] > 0) or seen[fingerprint] > 0:
                # Consume the match so a file carrying the same order twice
                # still imports its second copy once the first is on the ledger.
                seen[fingerprint] = max(seen[fingerprint] - 1, 0)
                if external is not None:
                    seen[external] = max(seen[external] - 1, 0)
                skips.append(AssetImportSkip(
                    row=order.row, ticker=order.ticker, reason='already_imported',
                    detail=f'{order.kind} {order.quantity} on {order.date.isoformat()}',
                ))
                continue

        held = units.get(order.ticker, Decimal('0'))
        if order.kind == 'sell' and order.quantity > held:
            errors.append(AssetImportRowError(
                row=order.row, reason='oversell', ticker=order.ticker,
                detail=f'selling {order.quantity} with {held} held',
            ))
            continue

        units[order.ticker] = held + (order.quantity if order.kind == 'buy' else -order.quantity)
        accepted.append(order)

    warnings.extend(_reconciliation_warnings(accepted, holdings, with_ledger, reported, units))
    unpriced = sorted({
        o.ticker for o in accepted
        if o.ticker not in holdings and not resolvable.get(o.ticker, False)
    })
    warnings.extend(
        AssetImportWarning(ticker=ticker, reason='unpriced_holding')
        for ticker in unpriced
    )

    to_create = sorted({o.ticker for o in accepted if o.ticker not in holdings})
    summary = {
        'imported': len(accepted),
        # The preview shows these rather than the parsed rows: a lot report
        # line becomes a buy and a sell, and only one of the two may already
        # be on the ledger.
        'accepted': accepted,
        'skipped': len(skips),
        'skips': skips,
        'holdings_created': len(to_create),
        'holdings_matched': len({o.ticker for o in accepted if o.ticker in holdings}),
        'errors': errors,
        'warnings': warnings,
    }
    if dry_run or not accepted:
        return summary

    # The log exists before the rows so they can point at it, and is removed
    # again if the run ends up writing nothing.
    log = ImportLog(
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=None,
        entity='asset_orders',
        filename=filename or 'orders.csv',
        format='csv',
        transaction_count=0,
    )
    session.add(log)
    await session.flush()

    quotes = {}
    if to_create:
        provider = market_provider or get_market_price_provider()
        quotes = await provider.get_quotes(to_create)

    touched: dict[str, Asset] = {}
    written = 0
    for order in accepted:
        asset = holdings.get(order.ticker)
        if asset is None:
            quote = quotes.get(order.ticker)
            if quote is None and not allow_unpriced:
                # Resolvable a moment ago in the batch check, gone now. Report
                # it rather than inventing a holding with no price.
                errors.append(AssetImportRowError(row=order.row, reason='unknown_ticker', ticker=order.ticker))
                continue
            asset = _new_holding(user_id, workspace_id, group_id, order, quote)
            session.add(asset)
            await session.flush()
            holdings[order.ticker] = asset

        session.add(AssetTransaction(
            asset_id=asset.id,
            workspace_id=workspace_id,
            kind=order.kind,
            quantity=order.quantity,
            price=order.price,
            fee=order.fee or Decimal('0'),
            date=order.date,
            source='import',
            external_id=order.external_id,
            import_id=log.id,
            notes=order.notes,
        ))
        touched[order.ticker] = asset
        written += 1

    await session.flush()
    # Once per holding, not once per row: the recompute walks the whole ledger.
    for asset in touched.values():
        await asset_transaction_service.recompute_and_cache(session, asset)

    if written:
        log.transaction_count = written
        session.add(log)
    else:
        await session.delete(log)
    await session.commit()

    summary['errors'] = errors
    summary['imported'] = written
    summary['import_log_id'] = str(log.id) if written else None
    summary['holdings_created'] = len([t for t in to_create if t in touched])
    return summary


def _reconciliation_warnings(
    accepted: list[AssetOrderImport],
    holdings: dict[str, Asset],
    with_ledger: set[uuid.UUID],
    reported: dict[str, Decimal],
    units: dict[str, Decimal],
) -> list[AssetImportWarning]:
    """Where a seeded history disagrees with the quantity a provider reported.

    A Snapshot Holding carries a quantity and no Trades behind it (CONTEXT.md),
    and `recompute_and_cache` rewrites `units` from the ledger — so importing a
    partial history onto one silently replaces the provider's figure with the
    file's. The discrepancy is surfaced rather than resolved: which of the two
    is right is not something this module can know.
    """
    warnings = []
    for ticker in sorted({o.ticker for o in accepted if o.ticker in holdings}):
        asset = holdings[ticker]
        if asset.id in with_ledger or reported.get(ticker, Decimal('0')) <= 0:
            continue
        from_file = units[ticker] - reported[ticker]
        if from_file != reported[ticker]:
            warnings.append(AssetImportWarning(
                ticker=ticker, reason='units_differ_from_provider',
                imported_units=_plain(from_file),
                reported_units=_plain(reported[ticker]),
            ))
    return warnings


def _plain(value: Decimal) -> str:
    """A quantity without the trailing zeros the ledger's scale pads it with."""
    trimmed = value.normalize()
    return f'{trimmed:f}'


def _new_holding(
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    group_id: Optional[uuid.UUID],
    order: AssetOrderImport,
    quote,
) -> Asset:
    """The holding an order opens, priced by the market or not priced at all."""
    if quote is None:
        return Asset(
            user_id=user_id,
            workspace_id=workspace_id,
            name=order.name or order.ticker,
            type=asset_transaction_service._type_from_quote(None, order.ticker),
            currency=(order.currency or 'USD').upper()[:3],
            # Not `market_price`: nothing will ever answer a quote for it, and
            # a market-priced holding with no price reads as a broken feed
            # rather than as an asset that genuinely has no market.
            valuation_method='manual',
            group_id=group_id,
            ticker=order.ticker,
            source='import',
        )
    return Asset(
        user_id=user_id,
        workspace_id=workspace_id,
        name=order.name or quote.name or order.ticker,
        type=asset_transaction_service._type_from_quote(quote.quote_type, order.ticker),
        # The quote's currency wins, exactly as when a holding is created by
        # hand: a file that reports an American stock in BRL would otherwise
        # label the holding BRL while its price feed keeps returning USD, and
        # the portfolio total drifts.
        currency=quote.currency,
        valuation_method='market_price',
        group_id=group_id,
        ticker=order.ticker,
        ticker_exchange=quote.exchange,
        last_price=Decimal(str(quote.price)),
        last_price_at=datetime.now(timezone.utc),
        logo_url=quote.logo_url,
        source='yfinance',
    )



def csv_template() -> str:
    """A file someone can fill in, with the required columns marked."""
    return (
        'ticker*,date*,quantity*,price*,fee,kind,currency,notes\n'
        'AAPL,2026-01-15,10,150.00,1.20,buy,USD,\n'
        'AAPL,2026-03-02,-4,178.30,1.20,sell,USD,partial exit\n'
        'PETR4.SA,2026-02-10,100,38.50,2.90,buy,BRL,\n'
    )


async def undo_import(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    log: ImportLog,
) -> int:
    """Take back every order an import wrote, and leave the portfolio as it was.

    Deleting the rows is only half of it. A position is derived from its
    ledger, so each touched holding has to be recomputed, and a holding this
    import created from nothing has to go with it — otherwise undoing leaves
    an empty ticker sitting in the wallet. A holding that still has orders
    after the delete is one the user also fed by hand or by an earlier import,
    so it stays.
    """
    result = await session.execute(
        select(AssetTransaction).where(AssetTransaction.import_id == log.id)
    )
    rows = list(result.scalars().all())
    asset_ids = {row.asset_id for row in rows}

    for row in rows:
        await session.delete(row)
    await session.flush()

    for asset_id in asset_ids:
        asset = await session.get(Asset, asset_id)
        if asset is None or asset.workspace_id != workspace_id:
            continue
        remaining = await session.execute(
            select(AssetTransaction.id).where(AssetTransaction.asset_id == asset_id).limit(1)
        )
        if remaining.scalar_one_or_none() is None:
            await session.delete(asset)
        else:
            await asset_transaction_service.recompute_and_cache(session, asset)

    await session.delete(log)
    await session.commit()
    return len(rows)
