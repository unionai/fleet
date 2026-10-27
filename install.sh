#!/usr/bin/env bash
# Install the Fleet agent using the command supplied by the Union control plane.
set +x
set -euo pipefail

work_dir=''
context=''
stage='initialization'
diagnose=false
prepare_timeout=600
wait_timeout=1800
active_timeout=600
cluster=''
deadline=0
token_expires=0
http_status=''
rpc_code=''
http_retried=false
instructions_version=''
namespace=dataplane-agent
service=cloudidl.fleet.FleetService
progress_phase=''
progress_label=''
progress_since=0
progress_dot=0
progress_summary=0
progress_open=false
progress_status=false
observed_status=''
observed_since=0
status_unavailable=false
helm_ticker_pid=''

progress_close() {
  if [[ "$progress_open" == true ]]; then printf '\n' >&4; progress_open=false; fi
}

say() { progress_close; printf '%s\n' "$*"; }
fail() { progress_close; printf 'ERROR [%s]: %s\n' "$stage" "$*" >&2; exit 1; }
now() { date +%s; }
kube() { kubectl --context "$context" --request-timeout=15s "$@"; }

duration() {
  local seconds=$1
  if ((seconds < 0)); then seconds=0; fi
  if ((seconds < 60)); then printf '%ss' "$seconds"
  elif ((seconds % 60 == 0)); then printf '%sm' "$((seconds / 60))"
  else printf '%sm %ss' "$((seconds / 60))" "$((seconds % 60))"; fi
}

progress_render() {
  local current=$1
  progress_close
  printf '%s (waiting %s' "$progress_label" "$(duration "$((current - progress_since))")"
  if [[ "$progress_status" == true && -n "$observed_status" ]]; then
    if [[ "$status_unavailable" == true ]]; then
      printf '; last status change observed %s ago' "$(duration "$((current - observed_since))")"
    else
      printf '; no status change observed for %s' "$(duration "$((current - observed_since))")"
    fi
  fi
  printf ')'
  progress_open=true
  progress_dot=$current
  progress_summary=$current
}

progress_tick() {
  local current
  current=$(now)
  if ((current - progress_dot >= 10)); then printf '.'; progress_dot=$current; fi
  if ((current - progress_summary >= 60)); then progress_render "$current"; fi
}

progress_wait() {
  local current
  current=$(now)
  if [[ "$progress_phase" != "$1" ]]; then
    progress_phase=$1
    progress_label=$2
    progress_since=$current
    progress_render "$current"
  elif [[ "$progress_open" == false ]]; then progress_render "$current"
  else progress_tick; fi
}

start_helm_progress() {
  progress_status=false
  progress_wait helm 'Installing the cluster agent'
  (
    trap - EXIT
    trap 'if [[ -n "${!:-}" ]]; then kill "$!" 2>/dev/null || :; wait "$!" 2>/dev/null || :; fi; exit 0' INT TERM
    while true; do
      sleep 10 &
      wait "$!" || exit 0
      progress_tick
    done
  ) &
  helm_ticker_pid=$!
}

stop_helm_progress() {
  if [[ -n "$helm_ticker_pid" ]]; then
    kill "$helm_ticker_pid" 2>/dev/null || :
    wait "$helm_ticker_pid" 2>/dev/null || :
    helm_ticker_pid=''
  fi
}

observe_status() {
  local summary
  summary=$(jq -r '
    def phase:
      if . == null then "waiting"
      elif .state == "LIFECYCLE_STATE_INSTALLING" or .state == 1 then "installing"
      elif (.state == "LIFECYCLE_STATE_COMPLETED" or .state == 2) and
        (.result == "LIFECYCLE_RESULT_SUCCESS" or .result == 1) then "installed"
      elif (.state == "LIFECYCLE_STATE_COMPLETED" or .state == 2) and
        (.result == "LIFECYCLE_RESULT_FAILURE" or .result == 2) then "failed"
      else "not yet confirmed" end;
    .status |
    (.agentInstallation | phase) as $agent |
    (.unionInstallation | phase) as $union |
    (if .connectivity == "CLUSTER_CONNECTIVITY_CONNECTED" or .connectivity == 1 then "connected"
      elif .connectivity == "CLUSTER_CONNECTIVITY_DISCONNECTED" or .connectivity == 2 then "disconnected"
      else "not yet confirmed" end) as $connection |
    (if .health == "CLUSTER_HEALTH_HEALTHY" or .health == 1 then "healthy"
      elif .health == "CLUSTER_HEALTH_UNHEALTHY" or .health == 2 then "not healthy yet"
      else "not yet reported" end) as $health |
    "Cluster agent: \($agent); Union services: \($union); connection: \($connection); health: \($health)."
  ' "$work_dir/status.json")
  if [[ "$summary" != "$observed_status" ]]; then
    observed_status=$summary
    observed_since=$(now)
    say "$summary"
  fi
}

usage() {
  cat <<'USAGE'
Usage: install.sh --cluster NAME [--wait-timeout SECONDS]

Export FLYTE_API_KEY for the Union control plane and organization that already
contain this Fleet cluster registration. An interactive terminal is required.

  --cluster NAME          Existing Fleet cluster name (required).
  --wait-timeout SECONDS  Override both preparation and post-Helm wait limits.
                         Defaults: preparation 600s; post-Helm 1800s.
  --help                  Show this help.
USAGE
}

confirm() {
  local answer
  printf '%s [y/N] ' "$1" >&3
  IFS= read -r answer <&3 || fail 'Could not read confirmation from the terminal.'
  case "$answer" in y|Y|yes|YES) ;; *) say 'Cancelled; no installation changes made.'; exit 0 ;; esac
}

diagnostics() {
  say "Agent diagnostics for context $context, namespace $namespace:"
  if kube get pods --namespace "$namespace" --output=json > "$work_dir/pods.json" 2>/dev/null; then
    jq -r '.items[]? | [.metadata.name, .status.phase,
      ([.status.containerStatuses[]? | .state.waiting.reason // .state.terminated.reason // empty] | join(","))]
      | @tsv' "$work_dir/pods.json"
  fi
  if kube get events --namespace "$namespace" --output=json > "$work_dir/events.json" 2>/dev/null; then
    jq -r '.items[]? | select(.type == "Warning") |
      [.involvedObject.kind, .involvedObject.name, .reason] | @tsv' "$work_dir/events.json"
  fi
  say 'Installed resources were preserved. Resolve the reported problem, then rerun the installer.'
}

finish() {
  local result=$?
  trap - EXIT INT TERM
  set +e
  stop_helm_progress
  progress_close
  if [[ "$result" != 0 && "$diagnose" == true && -n "$work_dir" ]]; then diagnostics; fi
  if [[ -n "$work_dir" ]]; then rm -rf -- "$work_dir"; fi
  exit "$result"
}

remaining() {
  local seconds
  seconds=$((deadline - $(now)))
  ((seconds > 0)) || fail "Timed out after $active_timeout seconds."
  printf '%s' "$seconds"
}

pause() {
  local seconds
  remaining >/dev/null
  seconds=$(remaining)
  if ((seconds > 5)); then seconds=5; fi
  sleep "$seconds"
}

# Responses and authentication headers stay in the private working directory.
http() {
  local method=$1 url=$2 auth=$3 body=$4 retries=$5 attempt seconds
  local -a args
  http_retried=false
  for ((attempt=1; attempt<=retries; attempt++)); do
    remaining >/dev/null
    seconds=$(remaining)
    if ((seconds > 30)); then seconds=30; fi
    args=(--silent --proto '=https' --connect-timeout 10 --max-time "$seconds"
      --request "$method" --output "$work_dir/response.json" --write-out '%{http_code}')
    if [[ -n "$auth" ]]; then args+=(--config "$auth"); fi
    if [[ -n "$body" ]]; then
      args+=(--header 'Content-Type: application/json' --data-binary "@$body")
    fi
    if ! http_status=$(curl "${args[@]}" "$url" 2> "$work_dir/http-error"); then http_status=000; fi
    case "$http_status" in
      000|429|500|502|503|504)
        if ((attempt < retries)); then http_retried=true; pause; continue; fi ;;
    esac
    return 0
  done
}

authenticate() {
  local token_url basic access_token expires
  http GET "https://$api_host/.well-known/oauth-authorization-server" '' '' 3
  [[ "$http_status" == 200 ]] || fail "OAuth discovery failed (HTTP $http_status)."
  token_url=$(jq -er '.token_endpoint // .tokenEndpoint | select(type == "string")' "$work_dir/response.json") \
    || fail 'OAuth discovery did not return a token endpoint.'
  [[ "$token_url" == https://* ]] || fail 'The OAuth token endpoint must use HTTPS.'
  basic=$(printf '%s:%s' "$client_id" "$client_secret" | base64 | tr -d '\r\n')
  printf 'header = "Authorization: Basic %s"\nheader = "Content-Type: application/x-www-form-urlencoded"\ndata = "grant_type=client_credentials&scope=all"\n' \
    "$basic" > "$work_dir/oauth.conf"
  http POST "$token_url" "$work_dir/oauth.conf" '' 3
  [[ "$http_status" == 200 ]] || fail "Authentication failed (HTTP $http_status); check FLYTE_API_KEY."
  access_token=$(jq -er '.access_token | select(type == "string" and length > 0)' "$work_dir/response.json") \
    || fail 'Authentication did not return an access token.'
  [[ "$access_token" =~ ^[A-Za-z0-9._~+/-]+=*$ ]] || fail 'Authentication returned an invalid bearer token.'
  printf 'header = "flyte-authorization: Bearer %s"\n' "$access_token" > "$work_dir/bearer.conf"
  expires=$(jq -r '.expires_in // 300 | tonumber | floor' "$work_dir/response.json") \
    || fail 'Authentication returned an invalid token lifetime.'
  ((expires > 0)) || fail 'Authentication returned an expired token.'
  if ((expires > 60)); then expires=$((expires - 30)); fi
  token_expires=$(($(now) + expires))
  rm -f -- "$work_dir/oauth.conf" "$work_dir/response.json"
}

rpc() {
  local procedure=$1 body=$2 retries=${3:-3}
  rpc_code=''
  if (($(now) >= token_expires)); then authenticate; fi
  http POST "https://$api_host/$service/$procedure" "$work_dir/bearer.conf" "$body" "$retries"
  if [[ "$http_status" == 200 ]]; then
    jq -e 'type == "object"' "$work_dir/response.json" >/dev/null 2>&1 \
      || fail "$procedure returned an invalid response."
    return 0
  fi
  rpc_code=$(jq -r '.code // "unknown" | select(type == "string")' "$work_dir/response.json" 2>/dev/null) || rpc_code=unknown
  case "$rpc_code" in
    unauthenticated|permission_denied|not_found|unavailable|aborted|failed_precondition|invalid_argument|resource_exhausted|internal) ;;
    *) rpc_code=unknown ;;
  esac
  return 1
}

rpc_failed() { fail "$1 failed (HTTP $http_status, $rpc_code). Check credentials, permissions and the cluster's console status."; }

get_status() {
  if ! rpc GetLifecycleStatus "$work_dir/cluster.json"; then return 1; fi
  jq -e --arg org "$organization" --arg name "$cluster" \
    '.status.clusterId.organization == $org and .status.clusterId.name == $name' "$work_dir/response.json" >/dev/null \
    || fail 'Lifecycle status does not match the requested Union target.'
  cp "$work_dir/response.json" "$work_dir/status.json"
}

phase_success() {
  jq -e --arg phase "$1" '.status[$phase] |
    (.state == "LIFECYCLE_STATE_COMPLETED" or .state == 2) and
    (.result == "LIFECYCLE_RESULT_SUCCESS" or .result == 1)' "$work_dir/status.json" >/dev/null
}

phase_failed() {
  jq -e --arg phase "$1" '.status[$phase] |
    (.state == "LIFECYCLE_STATE_COMPLETED" or .state == 2) and
    (.result == "LIFECYCLE_RESULT_FAILURE" or .result == 2)' "$work_dir/status.json" >/dev/null
}

check_union_failure() {
  if phase_failed unionInstallation; then
    fail 'Union installation has failed. Review the console diagnostics and recover it before rerunning; this installer does not retry failed Union deployments.'
  fi
}

preflight() {
  local resource verb denied=false nodes_ready
  local -a verbs scope
  kube get nodes --output=json > "$work_dir/nodes.json" 2> "$work_dir/kube-error" \
    || fail 'Cannot read cluster nodes. Check Kubernetes connectivity and permissions.'
  nodes_ready=$(jq -r 'any(.items[]?; .spec.unschedulable != true and
    any(.status.conditions[]?; .type == "Ready" and .status == "True"))' "$work_dir/nodes.json")
  [[ "$nodes_ready" == true ]] || fail 'No Ready, schedulable node is available.'
  if kube auth can-i '*' '*' --all-namespaces > /dev/null 2>&1; then return; fi
  for resource in deployments.apps serviceaccounts secrets clusterrolebindings.rbac.authorization.k8s.io priorityclasses.scheduling.k8s.io; do
    verbs=(get create update patch delete)
    scope=()
    case "$resource" in deployments.apps|serviceaccounts|secrets) scope=(--namespace "$namespace") ;; esac
    if [[ "$resource" == secrets || "$resource" == deployments.apps ]]; then verbs+=(list watch); fi
    for verb in "${verbs[@]}"; do
      if ! kube auth can-i "$verb" "$resource" ${scope[@]+"${scope[@]}"} >/dev/null 2>&1; then
        say "Missing permission: $verb $resource (namespace $namespace where applicable)"
        denied=true
      fi
    done
  done
  if ! kube auth can-i bind clusterroles.rbac.authorization.k8s.io/cluster-admin >/dev/null 2>&1; then
    say 'Missing permission: bind the cluster-admin role to the agent service account.'
    denied=true
  fi
  kube get namespace "$namespace" --ignore-not-found --output=json > "$work_dir/namespace.json" 2> "$work_dir/kube-error" \
    || fail 'Cannot check the agent namespace.'
  if [[ ! -s "$work_dir/namespace.json" ]] && ! kube auth can-i create namespaces >/dev/null 2>&1; then
    say "Missing permission: create namespace $namespace"
    denied=true
  fi
  [[ "$denied" == false ]] || fail 'Required agent-install permissions are missing.'
}

confirm_existing_agent() {
  local -a list_args=(list --namespace "$namespace" --output=json)
  if [[ "$helm_version" == v3.* ]]; then list_args+=(--all); fi
  kube get namespace "$namespace" --ignore-not-found --output=json \
    > "$work_dir/agent-namespace.json" 2> "$work_dir/kube-error" \
    || fail "Cannot inspect namespace $namespace."
  if [[ ! -s "$work_dir/agent-namespace.json" ]]; then return; fi
  jq -e --arg namespace "$namespace" 'type == "object" and .kind == "Namespace" and
    .metadata.name == $namespace' "$work_dir/agent-namespace.json" >/dev/null 2>&1 \
    || fail 'Kubernetes returned an invalid namespace response.'
  HELM_KUBECONTEXT=$context helm "${list_args[@]}" \
    > "$work_dir/releases.json" 2> "$work_dir/helm-error" \
    || fail 'Cannot inspect existing Helm releases.'
  jq -e 'type == "array" and all(.[]; .name | type == "string" and length > 0)' \
    "$work_dir/releases.json" >/dev/null 2>&1 || fail 'Helm returned an invalid release list.'
  kube get deployments.apps,daemonsets.apps,statefulsets.apps,pods --namespace "$namespace" --output=json \
    > "$work_dir/workloads.json" 2> "$work_dir/kube-error" \
    || fail "Cannot inspect existing workloads in namespace $namespace."
  jq -e --arg namespace "$namespace" 'type == "object" and (.items | type == "array") and
    all(.items[]; .metadata.namespace == $namespace and
      (.metadata.name | type == "string" and length > 0))' \
    "$work_dir/workloads.json" >/dev/null 2>&1 || fail 'Kubernetes returned an invalid workload list.'
  if jq -e 'length > 0' "$work_dir/releases.json" >/dev/null || \
    jq -e '.items | length > 0' "$work_dir/workloads.json" >/dev/null; then
    say 'Installing into a namespace that already contains an agent is not currently supported.' >&3
    confirm "Existing resources were found in namespace $namespace. Is proceeding with this Union target intentional?"
  fi
}

fetch_installation() {
  local procedure version agent_failed=false
  get_status || rpc_failed GetLifecycleStatus
  check_union_failure
  if phase_failed agentInstallation; then agent_failed=true; fi
  version=$(jq -er '.status.instructionsVersion | select(type == "string" and length > 0)' "$work_dir/status.json") \
    || fail 'The control plane did not return an instructions version.'
  procedure=GetAgentInstallationInstructions
  if [[ "$agent_failed" == true ]] || jq -e '.status.agentInstallation == null' "$work_dir/status.json" >/dev/null; then
    procedure=StartAgentInstallation
  fi
  jq --arg version "$version" '. + {instructionsVersion: $version}' "$work_dir/cluster.json" > "$work_dir/instructions.json"
  while true; do
    if [[ "$procedure" == StartAgentInstallation ]]; then
      if rpc "$procedure" "$work_dir/cluster.json"; then break; fi
    else
      if rpc "$procedure" "$work_dir/instructions.json"; then break; fi
    fi
    [[ "$rpc_code" == unavailable ]] || rpc_failed "$procedure"
    progress_wait instructions 'Waiting for the control plane to prepare agent instructions'
    pause
  done
  jq -e --arg version "$version" '.instructionsVersion == $version and
    (.values | type == "string" and length > 0) and
    (.helmCommand | type == "string" and length > 0)' "$work_dir/response.json" >/dev/null \
    || fail 'Agent instructions changed or are incomplete. Rerun to fetch current instructions.'
  instructions_version=$version
  jq -r '.values' "$work_dir/response.json" > "$work_dir/values.yaml"
  jq -r '.helmCommand' "$work_dir/response.json" > "$work_dir/agent-install.sh"
  rm -f -- "$work_dir/response.json"
}

agent_ready() {
  kube get deployment dp-agent --namespace "$namespace" --output=json > "$work_dir/agent.json" 2>/dev/null || return 1
  jq -e '(.spec.replicas // 1) as $wanted | $wanted > 0 and
    .status.observedGeneration >= .metadata.generation and
    .status.updatedReplicas == $wanted and .status.availableReplicas >= $wanted and
    .status.replicas == $wanted' "$work_dir/agent.json" >/dev/null
}

wait_for_union() {
  local requested=false stable_since=0 current installation_announced=false
  active_timeout=$wait_timeout
  deadline=$(($(now) + wait_timeout))
  progress_status=true
  while true; do
    remaining >/dev/null
    if ! get_status; then
      case "$http_status:$rpc_code" in
        000:*|429:*|500:*|502:*|503:*|504:*|*:unavailable)
          if ((stable_since > 0)); then
            say 'Readiness could not be verified. The 60-second stability check will restart.'
          fi
          stable_since=0
          if [[ "$status_unavailable" == false ]]; then
            say 'Unable to check installation status. Retrying.'
            if [[ -n "$observed_status" ]]; then say "Last reported status: $observed_status"; fi
          fi
          status_unavailable=true
          progress_wait unavailable 'Waiting for installation status to become available'
          pause; continue ;;
        *) rpc_failed GetLifecycleStatus ;;
      esac
    fi
    if [[ "$status_unavailable" == true ]]; then
      say 'Installation status checks are available again.'
      status_unavailable=false
    fi
    if [[ "$http_retried" == true ]]; then
      if ((stable_since > 0)); then
        say 'A status check needed a retry. The 60-second stability check will restart.'
        progress_phase=''
      fi
      stable_since=0
    fi
    jq -e --arg version "$instructions_version" '.status.instructionsVersion == $version' "$work_dir/status.json" >/dev/null \
      || fail 'Agent instructions changed during installation. Rerun to fetch current instructions.'
    check_union_failure
    if phase_failed agentInstallation; then fail 'Agent installation failed; inspect its pod conditions and console status.'; fi
    observe_status
    if phase_success agentInstallation && [[ "$requested" == false ]] && \
      jq -e '.status.unionInstallation == null' "$work_dir/status.json" >/dev/null; then
      stage='Union deployment'
      say 'Cluster agent installation is confirmed. Starting Union deployment...'
      requested=true
      rpc ApplyDeployment "$work_dir/cluster.json" 1 || rpc_failed ApplyDeployment
    fi
    stage='post-flight'
    if phase_success agentInstallation && phase_success unionInstallation; then
      if [[ "$installation_announced" == false ]]; then
        say 'The cluster agent and Union services are installed.'
        installation_announced=true
      fi
    fi
    if phase_success agentInstallation && phase_success unionInstallation && \
      jq -e '.status | (.connectivity == "CLUSTER_CONNECTIVITY_CONNECTED" or .connectivity == 1) and
        (.health == "CLUSTER_HEALTH_HEALTHY" or .health == 1)' "$work_dir/status.json" >/dev/null && agent_ready; then
      current=$(now)
      remaining >/dev/null
      if ((stable_since == 0)); then
        stable_since=$current
        progress_phase=''
        say 'Installation is healthy. Verifying 60 seconds of stability...'
      fi
      if ((current - stable_since >= 60)); then
        say "SUCCESS: $organization/$cluster is installed, connected and healthy (context $context)."
        return
      fi
      progress_wait stability 'Checking that the cluster stays healthy for 60 seconds'
    else
      if ((stable_since > 0)); then say 'Readiness changed. The 60-second stability check will restart.'; fi
      stable_since=0
      if ! phase_success agentInstallation; then
        progress_wait agent 'Waiting for Union to confirm the cluster agent installation'
      elif ! phase_success unionInstallation; then
        progress_wait union 'Waiting for Union services to finish installing'
      elif ! jq -e '.status.connectivity == "CLUSTER_CONNECTIVITY_CONNECTED" or .status.connectivity == 1' "$work_dir/status.json" >/dev/null; then
        progress_wait connection 'Installation is complete. Waiting for the cluster to connect'
      elif ! jq -e '.status.health == "CLUSTER_HEALTH_HEALTHY" or .status.health == 1' "$work_dir/status.json" >/dev/null; then
        progress_wait health 'Installation is complete. Waiting for the cluster to report healthy'
      else
        progress_wait ready 'Installation is complete. Waiting for the cluster agent to become ready'
      fi
    fi
    pause
  done
}

main() {
  # Preserve progress line endings when timeout checks discard stdout.
  exec 4>&1
  local tool decoded extra server helm_version
  while (($#)); do
    case "$1" in
      --cluster) (($# >= 2)) || fail '--cluster requires a name.'; cluster=$2; shift 2 ;;
      --wait-timeout) (($# >= 2)) || fail '--wait-timeout requires seconds.'; wait_timeout=$2; prepare_timeout=$2; shift 2 ;;
      --help|-h) usage; return ;;
      *) fail "Unknown argument: $1" ;;
    esac
  done
  [[ -n "$cluster" ]] || fail '--cluster is required.'
  [[ "$wait_timeout" =~ ^[1-9][0-9]{0,7}$ ]] || fail '--wait-timeout must be a positive number of seconds (at most eight digits).'
  for tool in curl kubectl helm jq base64 tr mktemp date sleep cp rm; do
    command -v "$tool" >/dev/null 2>&1 || fail "Required tool is missing: $tool. Install it before rerunning (see https://www.union.ai/docs/v2/union/user-guide/)."
  done
  [[ -n "${FLYTE_API_KEY:-}" ]] || fail 'Export FLYTE_API_KEY before running the installer.'
  decoded=$(printf '%s' "$FLYTE_API_KEY" | base64 -d 2>/dev/null) || fail 'FLYTE_API_KEY is not valid base64.'
  [[ "$decoded" != *$'\n'* && "$decoded" != *$'\r'* ]] || fail 'FLYTE_API_KEY contains invalid characters.'
  IFS=: read -r api_host client_id client_secret organization extra <<< "$decoded"
  [[ -n "$api_host" && -n "$client_id" && -n "$client_secret" && -n "$organization" && -z "$extra" && "$decoded" != *: ]] \
    || fail 'FLYTE_API_KEY must encode domain:client_id:client_secret:org.'
  [[ "$api_host" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || fail 'FLYTE_API_KEY must contain a control-plane hostname without a URL scheme or path.'
  unset decoded FLYTE_API_KEY
  umask 077
  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/union-fleet.XXXXXXXX")
  trap finish EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  stage='pre-flight'
  helm_version=$(helm version --short 2>/dev/null) || fail 'Cannot determine the Helm version.'
  [[ "$helm_version" == v3.* || "$helm_version" == v4.* ]] || fail 'Helm 3 or Helm 4 is required.'
  context=$(kubectl config current-context 2> "$work_dir/kube-error") || fail 'No current Kubernetes context is configured.'
  [[ -n "$context" ]] || fail 'No current Kubernetes context is configured.'
  server=$(kube config view --minify --output='jsonpath={.clusters[0].cluster.server}' 2> "$work_dir/kube-error") \
    || fail 'Cannot read the current Kubernetes API server.'
  [[ -n "$server" ]] || fail 'The current context has no Kubernetes API server.'
  if ! { exec 3<> /dev/tty; } 2>/dev/null; then fail 'An interactive terminal is required for context confirmation.'; fi
  say "Union target: $organization/$cluster (https://$api_host)" >&3
  say "Kubernetes context: $context" >&3
  say "Kubernetes API server: $server" >&3
  confirm "Is context $context the one you want to install into?"
  say 'Checking Kubernetes access and available nodes...'
  preflight
  confirm_existing_agent
  active_timeout=$prepare_timeout
  deadline=$(($(now) + prepare_timeout))
  jq -n --arg org "$organization" --arg name "$cluster" '{clusterId: {organization: $org, name: $name}}' > "$work_dir/cluster.json"
  say 'Authenticating with Union...'
  authenticate
  stage='agent instructions'
  progress_wait instructions 'Waiting for the control plane to prepare agent instructions'
  fetch_installation
  stage='agent installation'
  start_helm_progress
  diagnose=true
  if ! (cd "$work_dir"; export HELM_KUBECONTEXT="$context"; bash -e ./agent-install.sh) \
    > "$work_dir/helm-output" 2>&1; then
    stop_helm_progress
    fail 'The agent Helm command failed. Review agent diagnostics; no rollback was attempted.'
  fi
  stop_helm_progress
  say 'The cluster agent installation command finished. Waiting for confirmation from Union.'
  rm -f -- "$work_dir/values.yaml" "$work_dir/agent-install.sh" "$work_dir/helm-output"
  stage='agent connection'
  wait_for_union
}

main "$@"
