#!/usr/bin/env python3
"""discover.py 里不需要云的那部分的单元测试。

为什么值得测:这个脚本会**写** group_vars/all.yml。写坏一个值的代价不是报错,
是几十分钟后在某个不相干的地方失败。下面每一条都对应一个真出过问题、或者一眼
看不出来的边界:

  - cloud_env 里的 REGION 和顶层的 region 是两个键,靠缩进区分;串了就会把
    顶层 region 改成 "{{ region }}" 这种自指的值
  - 空值必须写成 ""。裸空值 YAML 解析成 None,而 nas_available 里的
    `ENDPOINT_NAS | default('') | length` 碰到 None 直接抛异常 —— 而「没有 NAS」
    恰恰是要用空值表达的正常情况
  - 被注释掉的键(nas_storage_type / bootimage_force_tcg)要能解开注释
  - 对等的两个网段重叠要能判出来

    python3 scripts/apsara/discover_test.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TEMPLATE = os.path.join(REPO, "ansible", "group_vars", "all.yml.apsara.example")

spec = importlib.util.spec_from_file_location("discover", os.path.join(HERE, "discover.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


print("set_key —— 缩进隔离")
lines = ["region: old\n", "cloud_env:\n", "  REGION: \"{{ region }}\"\n", "  AK: \"\"\n"]
d.set_key(lines, "AK", "LTAIsecret", indent=2)
check("cloud_env.AK 被改到", '  AK: LTAIsecret\n' in lines, lines)
check("顶层 region 没被 cloud_env.REGION 串改", lines[0] == "region: old\n", lines[0])
d.set_key(lines, "region", "cn-x-d01", indent=0)
check("顶层 region 能单独改", lines[0] == "region: cn-x-d01\n", lines[0])
check("cloud_env.REGION 仍是模板引用", lines[2].strip() == 'REGION: "{{ region }}"', lines[2])

print("set_key —— 空值")
lines = ['  ENDPOINT_NAS: "nas.old"\n']
d.set_key(lines, "ENDPOINT_NAS", "", indent=2)
check("空值写成双引号而不是裸空", lines[0].strip() == 'ENDPOINT_NAS: ""', lines[0])

print("set_key —— 注释掉的键")
lines = ["# nas_storage_type:  Capacity\n"]
d.set_key(lines, "nas_storage_type", "Performance", indent=0)
check("注释被解开", lines[0].strip() == "nas_storage_type: Performance", lines[0])

print("set_key —— 保留行尾注释")
lines = ['system_disk_category: ""          # ← 必填,例 cloud_pperf\n']
d.set_key(lines, "system_disk_category", "cloud_pperf", indent=0)
check("行尾注释保留", "# ← 必填" in lines[0] and "cloud_pperf" in lines[0], lines[0])

print("set_key —— 找不到的键要报 False,不能假装成功")
check("缺键返回 False", d.set_key(["a: 1\n"], "nope", "x", 0) is False)

print("cidr_overlaps")
check("10.0.0.0/16 与 10.0.0.0/16 重叠", d.cidr_overlaps("10.0.0.0/16", "10.0.0.0/16") is True)
check("192.168.0.0/16 与 10.0.0.0/16 不重叠",
      d.cidr_overlaps("192.168.0.0/16", "10.0.0.0/16") is False)
check("10.0.16.0/20 落在 10.0.0.0/16 里", d.cidr_overlaps("10.0.16.0/20", "10.0.0.0/16") is True)
check("畸形输入返回 None 而不是抛异常", d.cidr_overlaps("", "10.0.0.0/16") is None)

print("classify")
check("JSON + asapiSuccess 判成 ok", d.classify(0, '{"asapiSuccess": true}', "") == "ok")
check("InvalidAction 判成 reachable", d.classify(1, "", "InvalidAction.NotFound") == "reachable")
check("NeedSsl 判成 reachable", d.classify(1, "", "InvalidProtocol.NeedSsl") == "reachable")
check("no such host 判成 bad", d.classify(1, "", "dial tcp: no such host") == "bad")
check("503 判成 bad", d.classify(1, "", "503 Service Unavailable") == "bad")

print("pick_type —— 够用就行,不挑大的")
specs = {"s": (2, 4.0), "m": (4, 16.0), "l": (8, 32.0)}
check("满足下限里选最小", d.pick_type(["s", "m", "l"], specs, 4, 16) == "m")
check("都不满足时返回空", d.pick_type(["s"], specs, 4, 16) == "")
check("候选里有 specs 查不到的也不炸", d.pick_type(["unknown", "m"], specs, 4, 16) == "m")

print("dig —— 网关返回结构缺层时不抛")
check("正常取值", d.dig({"a": {"b": 1}}, "a", "b") == 1)
check("缺层返回 default", d.dig({"a": {}}, "a", "b", default="x") == "x")
check("中间不是 dict 也返回 default", d.dig({"a": 1}, "a", "b", default="x") == "x")

print("对真实模板做一次完整写入")
if os.path.exists(TEMPLATE):
    lines = open(TEMPLATE).readlines()
    written = {
        "region": "cn-wulan-ste2-d01", "zone": "cn-wulan-ste2-a", "zone2": "cn-wulan-ste2-a",
        "apsara_image_id": "aliyun_3_x86_64_20G_alibase_20241103.vhd",
        "control_plane_type": "ecs.g6.xlarge", "compute_type": "ecs.g6.xlarge",
        "worker_instance_type": "ecs.g6.large", "mirror_instance_type": "ecs.g6.xlarge",
        "system_disk_category": "cloud_pperf",
        "apsara_oss_endpoint": "https://oss-x.cloud.ste2.com",
        "apsara_peer_operator_vpc_id": "vpc-abc",
        "nas_storage_type": "Capacity", "bootimage_force_tcg": "true",
    }
    env_written = {
        "AK": "LTAIx", "SK": "sk", "ORG_ID": "org-1", "RG_ID": "rs-1",
        "ENDPOINT_ROS": "ros.cloud.ste2.com", "ENDPOINT_ECS": "ecs-internal.cloud.ste2.com",
        "ENDPOINT_VPC": "vpc.cloud.ste2.com", "ENDPOINT_RAM": "ram.cloud.ste2.com",
        "ENDPOINT_CLOUDDNS": "dns-control.pop.cloud.ste2.com",
        "ENDPOINT_SLB": "", "ENDPOINT_NAS": "",
    }
    missed = [k for k, v in written.items() if not d.set_key(lines, k, v, 0)]
    missed += [k for k, v in env_written.items() if not d.set_key(lines, k, v, 2)]
    check("模板里每个键都命中", not missed, f"没命中:{missed}")
    try:
        import yaml
        doc = yaml.safe_load("".join(lines))
        check("产物是合法 YAML", isinstance(doc, dict))
        check("顶层 region 正确", doc.get("region") == "cn-wulan-ste2-d01", doc.get("region"))
        check("cloud_env.REGION 仍引用顶层",
              doc["cloud_env"]["REGION"] == "{{ region }}", doc["cloud_env"].get("REGION"))
        check("ENDPOINT_NAS 是空串不是 None",
              doc["cloud_env"]["ENDPOINT_NAS"] == "", repr(doc["cloud_env"]["ENDPOINT_NAS"]))
        # nas_available 的判据:`| default('') | length == 0`。None 会在这里抛。
        check("空 ENDPOINT_NAS 能安全取 length",
              len(doc["cloud_env"]["ENDPOINT_NAS"] or "") == 0)
        check("site-apsara 的必填断言会通过",
              all(len(str(doc["cloud_env"].get(k) or "")) > 0
                  for k in ("AK", "SK", "ENDPOINT_ROS", "ENDPOINT_ECS",
                            "ENDPOINT_VPC", "ENDPOINT_CLOUDDNS")))
    except ImportError:
        print("  skip 没有 PyYAML,跳过解析检查")
else:
    print(f"  skip 找不到模板 {TEMPLATE}")

print()
if failures:
    print(f"FAILED: {len(failures)} 项 —— {failures}")
    sys.exit(1)
print("all ok")
