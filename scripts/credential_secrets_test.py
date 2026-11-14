#!/usr/bin/env python3
"""Exercise the real Secret tasks with Ansible and a fake oc; no cluster needed."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
AK = "FAKE-CREDENTIAL-ID-'\"$();\\\n雪"
SK = "FAKE-CREDENTIAL-SECRET-'\"$();\\\n雪"

FAKE_OC = r'''#!/usr/bin/env python3
import base64, json, os, pathlib, sys
root = pathlib.Path(__file__).parent
expected = json.loads((root / "expected.json").read_text())
body = json.load(sys.stdin)
assert sys.argv[1:] == ["--kubeconfig", "/fake kubeconfig", "apply", "-f", "-"]
assert body["apiVersion"] == "v1" and body["kind"] == "Secret"
assert body["type"] == "Opaque"
assert body["metadata"]["namespace"] == {"alibaba-creds": "capa-system", "alibaba-csi-creds": "kube-system", "custom-ccm-creds": "alibaba-cloud-controller-manager"}[body["metadata"]["name"]]
assert "stringData" not in body
decoded = {k: base64.b64decode(v, validate=True).decode() for k, v in body["data"].items()}
assert decoded == expected[body["metadata"]["name"]], "Secret payload mismatch"
needles = list(decoded.values()) + list(body["data"].values())
pid = os.getpid()
for _ in range(4):
    proc = pathlib.Path("/proc") / str(pid)
    if not proc.exists():
        break
    cmd = (proc / "cmdline").read_bytes()
    assert not any(n.encode() in cmd for n in needles if n), "Credential in process argv"
    pid = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[1])
assert not any(v in os.environ.values() for v in decoded.values()), "Credential inherited through environment"
mode = os.environ.get("MOCK_MODE", "success")
if mode != "success":
    # Deliberately echo the payload on failure: no_log must still protect it.
    print(json.dumps(body), file=sys.stderr)
    print('namespaces "missing" not found' if mode == "missing" else "Forbidden", file=sys.stderr)
    sys.exit(1)
state = root / (body["metadata"]["name"] + ".json")
new = json.dumps(body, sort_keys=True)
verb = "unchanged" if state.exists() and state.read_text() == new else "configured" if state.exists() else "created"
state.write_text(new)
print("secret/" + body["metadata"]["name"] + " " + verb)
'''


def find_task(path, name):
    def walk(value):
        if isinstance(value, dict):
            if value.get("name") == name:
                return value
            for nested in value.values():
                result = walk(nested)
                if result is not None:
                    return result
        elif isinstance(value, list):
            for nested in value:
                result = walk(nested)
                if result is not None:
                    return result
        return None
    task = walk(yaml.safe_load((ROOT / path).read_text()))
    assert task is not None, name
    return task


def main():
    specs = [
        ("ansible/playbooks/08-deploy-post-install.yml", "Create/update alibaba-creds secret for the CAPA controller (envFrom)", "_capa_secret", "alibaba-creds", "capa-system"),
        ("ansible/playbooks/08-deploy-post-install.yml", "08 — credentials Secret for the CSI driver (apsara)", "_csi_creds", "alibaba-csi-creds", "kube-system"),
        ("ansible/tasks/ccm_credentials_secret.yml", "{{ ccm_secret_caller | default('ccm') }} — credentials Secret", "_ccm_secret", "custom-ccm-creds", "alibaba-cloud-controller-manager"),
        ("ansible/playbooks/11-capa-routeb-join.yml", "Ensure alibaba-creds secret exists", "_routeb_secret", "alibaba-creds", "capa-system"),
    ]
    with tempfile.TemporaryDirectory(prefix="credential-secrets-") as tmp:
        folder = Path(tmp)
        oc = folder / "oc"
        oc.write_text(FAKE_OC)
        oc.chmod(0o700)
        plays = []
        for index, (path, name, register, secret, namespace) in enumerate(specs):
            task = copy.deepcopy(find_task(path, name))
            # Keep stdin, conditions, no_log and environment from the actual task.
            assert task.get("no_log") is True
            assert "ansible.builtin.shell" not in task
            argv = task["ansible.builtin.command"]["argv"]
            assert argv[0] == "oc"
            argv[0] = str(oc)
            # Only substitute the executable and kubeconfig; preserve real flags.
            if argv[2] == "/root/kubeconfig":
                argv[2] = "/fake kubeconfig"
            task["register"] = register
            tasks = []
            base_vars = dict(capa_ak=AK, capa_sk=SK, rb_ak=AK, rb_sk=SK,
                             kubeconfig_file="/fake kubeconfig", cloud_platform="apsara",
                             ccm_credentials_secret="custom-ccm-creds",
                             cloud_env={"AK": AK, "SK": SK, "OTHER_TOKEN": SK},
                             _capa_apsara_env={})
            variants = [{}, {"ALIBABA_CLOUD_ENDPOINT_ECS": "ecs.internal", "ALIBABA_CLOUD_ORGANIZATION_ID": "0123"}] if index == 0 else [{}]
            for extras in variants:
                for mode in ("success", "missing", "forbidden") if index == 2 else ("success",):
                    for optional in (True, False) if mode == "missing" else (False,):
                        values = ({"ALIBABA_CLOUD_ACCESS_KEY_ID": AK, "ALIBABA_CLOUD_ACCESS_KEY_SECRET": SK} | extras) if index in (0, 3) else {"access_key_id": AK, "access_key_secret": SK}
                        # Expected fixture contains only synthetic test credentials.
                        tasks.append({"name": "Set fake oc expectation", "ansible.builtin.copy": {"dest": str(folder / "expected.json"), "content": json.dumps({secret: values}), "mode": "0600"}, "no_log": True})
                        for repetition in range(2 if mode == "success" else 1):
                            current = copy.deepcopy(task)
                            current["vars"] = {"_capa_apsara_env": extras, "_ccm_secret_optional": optional}
                            # Preserve Route B's blanking dictionary and add a non-secret test control.
                            original_env = current.get("environment")
                            current["environment"] = "{{ (" + original_env.strip()[2:-2].strip() + ") | combine({'MOCK_MODE': '" + mode + "'}) }}" if original_env else {"MOCK_MODE": mode}
                            current["ignore_errors"] = True
                            tasks.append(current)
                            failed = mode == "forbidden" or (mode == "missing" and not optional)
                            conditions = [f"{register} is {'failed' if failed else 'not failed'}"]
                            if mode == "success":
                                conditions.append(f"'unchanged' in {register}.stdout" if repetition else f"'unchanged' not in {register}.stdout")
                                conditions.append(f"{register}.changed == {'true' if index == 3 or repetition == 0 else 'false'}")
                            tasks.append({"name": "Check actual command result", "ansible.builtin.assert": {"that": conditions}, "no_log": True})
                # State isolation between the public/apsara variants and Route B.
                tasks.append({"name": "Clear fake Secret state", "ansible.builtin.file": {"path": str(folder / (secret + ".json")), "state": "absent"}})
            play = {"name": f"Secret path {index}", "hosts": "localhost", "connection": "local", "gather_facts": False, "vars": base_vars, "tasks": tasks}
            if index == 3:
                play["environment"] = "{{ cloud_env }}"
            plays.append(play)
        book = folder / "test.yml"
        book.write_text(yaml.safe_dump(plays, sort_keys=False, allow_unicode=True))
        env = os.environ.copy()
        env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOG_PATH=str(folder / "ansible.log"))
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(book)], cwd=folder, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = result.stdout + (folder / "ansible.log").read_text()
        for token in (AK, SK, base64.b64encode(AK.encode()).decode(), base64.b64encode(SK.encode()).decode()):
            assert token not in output, "Credential exposed in Ansible output/log"
        if result.returncode:
            print(result.stdout)
            raise SystemExit(result.returncode)
        print("PASS: four real Secret tasks; public/apsara payloads; repeat-apply change detection; optional namespace and hard failures; no argv/environment/log credential exposure")


if __name__ == "__main__":
    main()
