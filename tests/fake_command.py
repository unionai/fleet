#!/usr/bin/env python3
"""Offline command doubles. No network or Kubernetes calls leave this process."""

import json
import os
from pathlib import Path
import sys

root = Path(os.environ["FAKE_ROOT"])
options = json.loads((root / "options.json").read_text())
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as output:
    output.write(json.dumps({"tool": tool, "args": args,
                             "context": os.environ.get("HELM_KUBECONTEXT"),
                             "cwd": os.getcwd()}) + "\n")


def state(name, default=0):
    path = root / name
    return int(path.read_text()) if path.exists() else default


def increment(name):
    value = state(name) + 1
    (root / name).write_text(str(value))
    return value


def emit(value):
    print(json.dumps(value))


def phase(success=True):
    return {"state": "LIFECYCLE_STATE_COMPLETED",
            "result": "LIFECYCLE_RESULT_SUCCESS" if success else "LIFECYCLE_RESULT_FAILURE"}


def lifecycle():
    poll = increment("polls")
    installed = (root / "installed").exists()
    result = {"clusterId": {"organization": "test-org", "name": "test-cluster"},
              "instructionsVersion": "instructions-1"}
    if options.get("wrong_target"):
        result["clusterId"]["name"] = "wrong-cluster"
    if options.get("existing") or options.get("phase_open"):
        result["agentInstallation"] = {"state": "LIFECYCLE_STATE_INSTALLING"}
    if installed:
        if options.get("instructions_changed_after_install"):
            result["instructionsVersion"] = "instructions-2"
        result["agentInstallation"] = phase(not options.get("agent_failure"))
        result["connectivity"] = "CLUSTER_CONNECTIVITY_CONNECTED"
        result["health"] = "CLUSTER_HEALTH_HEALTHY"
        if (root / "applied").exists() or options.get("union_existing"):
            result["unionInstallation"] = phase(not options.get("union_failure"))
        if options.get("wait_forever"):
            result.pop("agentInstallation", None)
        if poll == options.get("unhealthy_poll"):
            result["health"] = "CLUSTER_HEALTH_UNHEALTHY"
    if options.get("union_failed_before"):
        result["unionInstallation"] = phase(False)
    elif options.get("union_existing") and not installed:
        result["agentInstallation"] = phase()
        result["unionInstallation"] = {"state": "LIFECYCLE_STATE_INSTALLING"}
    if options.get("numeric_enums"):
        for field in ("agentInstallation", "unionInstallation"):
            if field in result:
                item = result[field]
                item["state"] = 2 if item["state"] == "LIFECYCLE_STATE_COMPLETED" else 1
                if "result" in item:
                    item["result"] = 1 if item["result"] == "LIFECYCLE_RESULT_SUCCESS" else 2
        if "health" in result:
            result["health"] = 1 if result["health"] == "CLUSTER_HEALTH_HEALTHY" else 2
            result["connectivity"] = 1
    return {"status": result}


if tool == "date":
    print(state("clock", 1000))
elif tool == "sleep":
    (root / "clock").write_text(str(state("clock", 1000) + int(float(args[0]))))
elif tool == "kubectl":
    if args == ["config", "current-context"]:
        print("test-context")
        sys.exit(0)
    if args[:3] != ["--context", "test-context", "--request-timeout=15s"]:
        sys.exit("kubectl did not pin the confirmed context")
    args = args[3:]
    if args[:2] == ["config", "view"]:
        print("https://kubernetes.example.test")
    elif args[:2] == ["get", "nodes"]:
        if options.get("api_unreachable"):
            sys.exit(1)
        emit({"items": [{"spec": {"unschedulable": False}, "status": {"conditions": [
            {"type": "Ready", "status": "False" if options.get("no_nodes") else "True"}]}}]})
    elif args[:2] == ["auth", "can-i"]:
        if options.get("rbac_denied"):
            sys.exit(1)
        if options.get("limited_rbac") and args[2] == "*":
            sys.exit(1)
        print("yes")
    elif args[:2] == ["get", "namespace"]:
        if options.get("namespace_error"):
            sys.exit("PRIVATE-KEY-SENTINEL")
        if "namespace_response" in options:
            emit(options["namespace_response"])
            sys.exit(0)
        if not options.get("namespace_absent"):
            emit({"kind": "Namespace", "metadata": {"name": "dataplane-agent"}})
    elif args[:2] == ["get", "deployments.apps,daemonsets.apps,statefulsets.apps,pods"]:
        if args[2:] != ["--namespace", "dataplane-agent", "--output=json"]:
            sys.exit("workload lookup did not use the agent namespace")
        if options.get("workload_list_error"):
            sys.exit("PRIVATE-KEY-SENTINEL")
        if "workload_response" in options:
            emit(options["workload_response"])
        else:
            emit({"items": [item for item in options.get("workloads", [])
                            if item["metadata"]["namespace"] == "dataplane-agent"]})
    elif args[:2] == ["get", "deployment"]:
        (root / "readiness-time").write_text(str(state("clock", 1000)))
        if options.get("agent_not_ready"):
            sys.exit(1)
        emit({"metadata": {"generation": 1}, "spec": {"replicas": 1}, "status": {
            "observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 1}})
    elif args[:2] == ["get", "pods"]:
        emit({"items": [{"metadata": {"name": "dp-agent-123"}, "status": {"phase": "Pending",
             "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}]}}]})
    elif args[:2] == ["get", "events"]:
        emit({"items": [{"type": "Warning", "reason": "FailedScheduling",
             "involvedObject": {"kind": "Pod", "name": "dp-agent-123"},
             "message": "PRIVATE-KEY-SENTINEL"}]})
    else:
        sys.exit(f"unexpected fake kubectl arguments: {args}")
elif tool == "helm":
    if args == ["version", "--short"]:
        print(options.get("helm_version", "v3.19.0+test"))
    elif args[0] == "list":
        if os.environ.get("HELM_KUBECONTEXT") != "test-context":
            sys.exit("Helm release lookup did not pin the confirmed context")
        if args != ["list", "--namespace", "dataplane-agent", "--all", "--output=json"]:
            sys.exit("Helm lookup must include every release in the agent namespace")
        if options.get("helm_list_error"):
            sys.exit("PRIVATE-KEY-SENTINEL")
        if "helm_response" in options:
            emit(options["helm_response"])
        else:
            releases = options.get("releases", [{"name": "dp-agent", "namespace": "dataplane-agent"}]
                                   if options.get("existing") else [])
            emit([release for release in releases if release["namespace"] == "dataplane-agent"])
    elif args[:2] == ["upgrade", "--install"]:
        if os.environ.get("HELM_KUBECONTEXT") != "test-context":
            sys.exit("Helm install did not pin the confirmed context")
        if "PRIVATE-KEY-SENTINEL" not in Path("values.yaml").read_text():
            sys.exit("missing API-provided values")
        if (Path("values.yaml").stat().st_mode & 0o077) != 0:
            sys.exit("values file is not private")
        if "FLYTE_API_KEY" in os.environ:
            sys.exit("API key was inherited by Helm")
        if options.get("helm_failure"):
            print("PRIVATE-KEY-SENTINEL", file=sys.stderr)
            sys.exit(1)
        (root / "installed").touch()
        (root / "helm-time").write_text(str(state("clock", 1000)))
    else:
        sys.exit(f"unexpected fake helm arguments: {args}")
elif tool == "curl":
    url = args[-1]
    output = Path(args[args.index("--output") + 1])
    config = Path(args[args.index("--config") + 1]).read_text() if "--config" in args else ""
    status = 200
    body = {}
    if url.endswith("/.well-known/oauth-authorization-server"):
        body = {"token_endpoint": "https://control.example.test/auth/token"}
    elif url.endswith("/auth/token"):
        if "Authorization: Basic " not in config:
            sys.exit("OAuth request did not carry Basic auth")
        if options.get("auth_failure"):
            status, body = 401, {"error_description": "CLIENT-SECRET-SENTINEL"}
        else:
            increment("auth-count")
            body = {"access_token": "ACCESS-TOKEN-SENTINEL", "expires_in": options.get("token_lifetime", 3600)}
    else:
        if "flyte-authorization: Bearer ACCESS-TOKEN-SENTINEL" not in config:
            sys.exit("RPC request did not carry the expected bearer token")
        request_path = Path(args[args.index("--data-binary") + 1][1:])
        request = json.loads(request_path.read_text())
        if request.get("clusterId") != {"organization": "test-org", "name": "test-cluster"}:
            sys.exit("RPC did not use the requested Union target")
        method = url.rsplit("/", 1)[1]
        if options.get("rpc_unauthenticated"):
            status, body = 401, {"code": "unauthenticated", "message": "ACCESS-TOKEN-SENTINEL"}
        elif method == "GetLifecycleStatus":
            if options.get("status_outage") and (root / "installed").exists():
                status, body = 503, {"code": "unavailable"}
            else:
                body = lifecycle()
        elif method in ("StartAgentInstallation", "GetAgentInstallationInstructions"):
            count = increment("instruction-count")
            if count <= options.get("instructions_unavailable", 0):
                status, body = 503, {"code": "unavailable"}
            elif options.get("instructions_aborted"):
                status, body = 409, {"code": "aborted"}
            else:
                command = "helm upgrade --install dp-agent oci://agent.example.test/chart --version 1.99.7 --namespace dataplane-agent --create-namespace --values values.yaml --set nameOverride=dp-agent --timeout 10m0s --wait"
                if options.get("command_suffix"):
                    command += '\nprintf "returned-command-was-executed" > "$FAKE_ROOT/command-proof"'
                body = {"instructionsVersion": "changed" if options.get("instructions_changed") else "instructions-1",
                        "values": "tls:\n  clientKey: PRIVATE-KEY-SENTINEL\n", "helmCommand": command}
        elif method == "ApplyDeployment":
            increment("apply-count")
            if options.get("apply_failure"):
                status, body = 500, {"code": "internal", "message": "PRIVATE-KEY-SENTINEL"}
            else:
                (root / "applied").touch()
                body = {"revision": {"revision": "1"}}
        else:
            sys.exit(f"unexpected RPC {method}")
    output.write_text(json.dumps(body))
    print(status, end="")
else:
    sys.exit(f"unexpected fake command: {tool}")
