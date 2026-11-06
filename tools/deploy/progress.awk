# Read only the current phase. Emit fixed labels, never text copied from logs.
BEGIN {
  wanted = mode == "build" ? "Build images" : "Start + health"
  split("backend frontend tax-planner celery-worker celery-beat mcp-server db redis", services, " ")
  # Exact tokens map to literals. Never echo even a partly matched log token.
  build_services["[backend"] = build_services["[backend]"] = "backend"
  build_services["[frontend"] = build_services["[frontend]"] = "frontend"
  build_services["[tax-planner"] = build_services["[tax-planner]"] = "tax-planner"
}
index($0, "] START " wanted) { in_phase = 1; next }
!in_phase { next }
mode == "build" && /^#[0-9]+ / {
  id = $1
  if ($2 == "DONE" || $2 == "CACHED" || $2 == "ERROR" || $2 == "CANCELED") {
    delete active[id]
    next
  }
  if ($2 in build_services) {
    service = build_services[$2]
    names[id] = service
    label = "building image"
    if ($0 ~ /load metadata/) label = "fetching base image metadata"
    else if ($0 ~ /load build context/) label = "sending build context"
    else if ($0 ~ /uv sync/ && $0 ~ /--compile-bytecode/) label = "installing dependencies and compiling bytecode"
    else if ($0 ~ /uv sync/) label = "installing Python packages"
    else if ($0 ~ /npm ci/) label = "installing Node dependencies"
    else if ($0 ~ /npm run build/) label = "building frontend assets"
    else if ($0 ~ /COPY/) label = "copying build inputs"
    else if ($0 ~ /exporting to image/) label = "exporting image"
    labels[id] = service ": " label
    active[id] = ++sequence
  } else if (id in names && $2 == "unpacking") {
    labels[id] = names[id] ": unpacking image"
    active[id] = ++sequence
  }
}
mode == "start" && $1 == "Container" {
  for (i in services) {
    service = services[i]
    # Container/project names stay private; only a known service suffix is used.
    if ($2 !~ ("-" service "-[0-9]+$")) continue
    if ($3 == "Running" || $3 == "Healthy") { delete active[service]; next }
    label = ""
    if ($3 == "Recreate") label = "recreating container"
    else if ($3 == "Recreated") label = "recreated; waiting for startup"
    else if ($3 == "Starting") label = "starting container"
    else if ($3 == "Started") label = "started; waiting for readiness"
    else if ($3 == "Waiting") label = "waiting for readiness"
    if (label != "") {
      labels[service] = service ": " label
      active[service] = ++sequence
    }
    next
  }
}
END {
  latest = 0
  detail = mode == "build" ? "waiting for Docker build to finish" : "waiting for Docker startup/readiness checks"
  for (id in active) if (active[id] > latest) { latest = active[id]; detail = labels[id] }
  print detail
}
