// apsara-rpc: 通用 Apsara Stack RPC-API 调用器,在 Apsara 模式下替代 `aliyun <product> <action>`。
// 用法: apsara-rpc <Product> <Version> <Action> [--Key val | --Key=val | --Key=@file] ...
// 环境: AK, SK[, STS_TOKEN], REGION, ORG_ID, RG_ID, APSARA_PROXY,
//       端点 = ENDPOINT 或 ENDPOINT_<PRODUCT大写>(如 ENDPOINT_ROS=ros.cloud.ste3.com)
//       SCHEME=https 走 HTTPS(RAM 必需);INSECURE=1 跳过内部 CA 证书校验。
package main

import (
	"crypto/tls"
	"fmt"
	"net/http"
	"net/url"
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

	// Install an explicit transport so HTTPS-through-proxy uses a real CONNECT
	// tunnel (end-to-end TLS).  The SDK's SetHttpsProxy path did NOT tunnel for
	// the CloudDns POP endpoint (dns-control.pop.cloud.ste3.com): the request
	// reached the POP as plain HTTP and was rejected (InvalidProtocol.NeedSsl).
	// A plain http.Transport with Proxy set does CONNECT for https URLs (exactly
	// like `curl -x <proxy> https://…`) and forwards http for http URLs, so the
	// existing HTTP products (ROS/ECS/VPC) keep working unchanged.
	insecure := os.Getenv("INSECURE") == "1"
	tr := &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: insecure}}
	if p := os.Getenv("APSARA_PROXY"); p != "" {
		u, e := url.Parse(p)
		if e != nil { die("bad APSARA_PROXY:", e) }
		tr.Proxy = func(req *http.Request) (*url.URL, error) {
			if os.Getenv("APSARA_RPC_DEBUG") == "1" {
				fmt.Fprintf(os.Stderr, "[apsara-rpc] transport req=%s (scheme=%s host=%s) -> proxy %s\n",
					req.URL.String(), req.URL.Scheme, req.URL.Host, u.String())
			}
			return u, nil
		}
	}
	client.SetTransport(tr)
	// DoAction re-stamps TLSClientConfig.InsecureSkipVerify from this flag on
	// every call, so keep it in sync or our transport's value gets reset to false.
	client.SetHTTPSInsecure(insecure)

	if os.Getenv("APSARA_RPC_DEBUG") == "1" {
		fmt.Fprintf(os.Stderr, "[apsara-rpc] product=%s version=%s action=%s endpoint=%s scheme=%s proxy=%q insecure=%v\n",
			product, version, action, endpoint, os.Getenv("SCHEME"), os.Getenv("APSARA_PROXY"), insecure)
	}

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
