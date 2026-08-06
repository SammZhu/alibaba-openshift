// apsara-rpc: 通用 Apsara Stack RPC-API 调用器,在 Apsara 模式下替代 `aliyun <product> <action>`。
// 用法: apsara-rpc <Product> <Version> <Action> [--Key val | --Key=val | --Key=@file] ...
// 环境: AK, SK[, STS_TOKEN], REGION, ORG_ID, RG_ID, APSARA_PROXY,
//       端点 = ENDPOINT 或 ENDPOINT_<PRODUCT大写>(如 ENDPOINT_ROS=ros.cloud.ste3.com)
//       SCHEME=https 走 HTTPS(RAM 必需);INSECURE=1 跳过内部 CA 证书校验。
package main

import (
	"fmt"
	"os"
	"strings"

	"github.com/aliyun/alibaba-cloud-sdk-go/sdk"
	"github.com/aliyun/alibaba-cloud-sdk-go/sdk/auth/credentials"
	"github.com/aliyun/alibaba-cloud-sdk-go/sdk/requests"
)

func env(k, d string) string { if v := os.Getenv(k); v != "" { return v }; return d }
func die(a ...interface{})   { fmt.Fprintln(os.Stderr, a...); os.Exit(1) }

func main() {
	if len(os.Args) < 4 {
		die("usage: apsara-rpc <Product> <Version> <Action> [--Key val|--Key=val|--Key=@file]...")
	}
	product, version, action := os.Args[1], os.Args[2], os.Args[3]
	region := env("REGION", "cn-wulan-ste3-d01")

	endpoint := os.Getenv("ENDPOINT")
	if endpoint == "" {
		endpoint = os.Getenv("ENDPOINT_" + strings.ToUpper(product))
	}
	if endpoint == "" {
		die("set ENDPOINT or ENDPOINT_" + strings.ToUpper(product) + " (e.g. ros.cloud.ste3.com)")
	}

	ak, sk, st := os.Getenv("AK"), os.Getenv("SK"), os.Getenv("STS_TOKEN")
	var cred interface{}
	if st != "" {
		cred = credentials.NewStsTokenCredential(ak, sk, st)
	} else {
		cred = credentials.NewAccessKeyCredential(ak, sk)
	}
	client, err := sdk.NewClientWithOptions(region, sdk.NewConfig(), cred)
	if err != nil { die("client:", err) }
	if p := os.Getenv("APSARA_PROXY"); p != "" { client.SetHttpProxy(p); client.SetHttpsProxy(p) }
	if os.Getenv("INSECURE") == "1" { client.SetHTTPSInsecure(true) }

	r := requests.NewCommonRequest()
	sc := requests.HTTP
	if os.Getenv("SCHEME") == "https" { sc = requests.HTTPS }
	r.SetScheme(sc)
	r.Product, r.Version, r.ApiName, r.Domain, r.Method = product, version, action, endpoint, "POST"
	r.Headers["x-acs-caller-sdk-source"] = "apsara-rpc"
	r.Headers["x-acs-regionid"] = region
	if v := os.Getenv("ORG_ID"); v != "" { r.Headers["x-acs-organizationid"] = v }
	if v := os.Getenv("RG_ID"); v != "" { r.Headers["x-acs-resourcegroupid"] = v }
	r.QueryParams["Action"] = action
	r.QueryParams["RegionId"] = region

	args := os.Args[4:]
	for i := 0; i < len(args); i++ {
		a := args[i]
		if !strings.HasPrefix(a, "--") { die("unexpected arg:", a) }
		a = a[2:]
		var k, val string
		if strings.Contains(a, "=") {
			kv := strings.SplitN(a, "=", 2); k, val = kv[0], kv[1]
		} else {
			k = a
			if i+1 < len(args) { val = args[i+1]; i++ }
		}
		if strings.HasPrefix(val, "@") {
			b, e := os.ReadFile(val[1:]); if e != nil { die("read", val[1:], ":", e) }; val = string(b)
		}
		if len(val) > 1024 || k == "TemplateBody" || k == "PolicyDocument" {
			r.FormParams[k] = val // 大参数走 body
		} else {
			r.QueryParams[k] = val
		}
	}
	r.SetContentType(requests.Form)

	resp, err := client.ProcessCommonRequest(r)
	if err != nil { die(err) }
	fmt.Println(resp.GetHttpContentString())
}
