// apsara-rpc: 通用 Apsara Stack RPC-API 调用器,在 Apsara 模式下替代 `aliyun <product> <action>`。
// 用法: apsara-rpc <Product> <Version> <Action> [--Key val | --Key=val | --Key=@file] ...
// 环境: AK, SK[, STS_TOKEN], REGION, ORG_ID, RG_ID, APSARA_PROXY,
//       端点 = ENDPOINT 或 ENDPOINT_<PRODUCT大写>(如 ENDPOINT_ROS=ros.cloud.ste3.com)
//       SCHEME=https 走 HTTPS(RAM 必需);INSECURE=1 跳过内部 CA 证书校验。
package main

import (
	"crypto/hmac"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"

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

	// Parse --Key val / --Key=val / --Key=@file into a param map.
	cli := map[string]string{}   // normal params (query)
	large := map[string]string{} // big params -> body on the SDK path
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
			large[k] = val
		} else {
			cli[k] = val
		}
	}

	// Native send path (NATIVE_SEND=1): sign with ACS Signature v1 and send with
	// a bare net/http client, bypassing the SDK's send path.  The CloudDns POP
	// (dns-control.pop.cloud.ste3.com) is HTTPS-only and reachable only through
	// the Squid proxy; over that CONNECT tunnel the SDK's own send is rejected
	// with InvalidProtocol.NeedSsl, while a plain http.Client over the identical
	// transport passes (verified).  Only cloudcli's POP products set this, so
	// ROS/ECS/VPC/RAM keep using the SDK unchanged.
	if os.Getenv("NATIVE_SEND") == "1" {
		for k, v := range large { cli[k] = v } // native puts everything in the query
		var px *url.URL
		if p := os.Getenv("APSARA_PROXY"); p != "" {
			if pu, e := url.Parse(p); e == nil { px = pu }
		}
		nativeSend(endpoint, version, action, region, cli, insecure, px)
		return
	}

	// SDK path (default): unchanged behaviour for ROS/ECS/VPC/RAM/...
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
	for k, v := range cli { r.QueryParams[k] = v }
	for k, v := range large { r.FormParams[k] = v } // 大参数走 body
	r.SetContentType(requests.Form)

	resp, err := client.ProcessCommonRequest(r)
	if err != nil { die(err) }
	fmt.Println(resp.GetHttpContentString())
}

// localIP returns this host's outbound IP (no packet sent — UDP "dial" just picks
// the routing interface).  Used as the SecureTransport SourceIp assertion.
func localIP() string {
	c, err := net.Dial("udp", "100.100.100.200:80")
	if err != nil { return "127.0.0.1" }
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).IP.String()
}

// acsEscape percent-encodes per ACS Signature v1 (RFC3986 + Aliyun tweaks).
func acsEscape(s string) string {
	e := url.QueryEscape(s)
	e = strings.ReplaceAll(e, "+", "%20")
	e = strings.ReplaceAll(e, "*", "%2A")
	e = strings.ReplaceAll(e, "%7E", "~")
	return e
}

// nativeSend signs an RPC request with ACS Signature v1 (HMAC-SHA1) and sends it
// with a plain net/http client.  See the NATIVE_SEND note in main() for why.
func nativeSend(endpoint, version, action, region string, cli map[string]string,
	insecure bool, proxy *url.URL) {

	ak, sk, st := os.Getenv("AK"), os.Getenv("SK"), os.Getenv("STS_TOKEN")

	p := map[string]string{}
	for k, v := range cli { p[k] = v }
	p["Action"] = action
	p["Version"] = version
	p["Format"] = "JSON"
	p["AccessKeyId"] = ak
	p["SignatureMethod"] = "HMAC-SHA1"
	p["SignatureVersion"] = "1.0"
	p["SignatureNonce"] = fmt.Sprintf("%d%d", time.Now().UnixNano(), os.Getpid())
	p["Timestamp"] = time.Now().UTC().Format("2006-01-02T15:04:05Z")
	if _, ok := p["RegionId"]; !ok { p["RegionId"] = region }
	if st != "" { p["SecurityToken"] = st }
	// The POP enforces its SSL requirement AFTER signature validation; when the
	// request is tunnelled through the proxy it must carry the SecureTransport
	// assertion as SIGNED QUERY PARAMS — SourceIp (non-empty) + SecureTransport=true
	// — exactly as the SDK does for RPC requests (see client.go DoAction). Headers
	// alone do NOT satisfy it (verified).
	src := os.Getenv("PROXY_SOURCE_IP")
	if src == "" { src = localIP() }
	p["SourceIp"] = src
	p["SecureTransport"] = "true"

	// canonicalized query = sorted key=acsEscape(value) joined by '&'
	keys := make([]string, 0, len(p))
	for k := range p { keys = append(keys, k) }
	sort.Strings(keys)
	parts := make([]string, 0, len(p))
	for _, k := range keys {
		parts = append(parts, acsEscape(k)+"="+acsEscape(p[k]))
	}
	canon := strings.Join(parts, "&")
	sts := "POST&" + acsEscape("/") + "&" + acsEscape(canon)
	mac := hmac.New(sha1.New, []byte(sk+"&"))
	mac.Write([]byte(sts))
	sig := base64.StdEncoding.EncodeToString(mac.Sum(nil))

	target := "https://" + endpoint + "/?" + canon + "&Signature=" + acsEscape(sig)
	if os.Getenv("NATIVE_DRYRUN") == "1" { fmt.Println(target); return } // print signed URL, don't send
	req, err := http.NewRequest("POST", target, nil)
	if err != nil { die("native build:", err) }
	if os.Getenv("NATIVE_MINIMAL_HEADERS") != "1" {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		req.Header.Set("x-acs-caller-sdk-source", "apsara-rpc")
		req.Header.Set("x-acs-regionid", region)
		if v := os.Getenv("ORG_ID"); v != "" { req.Header.Set("x-acs-organizationid", v) }
		if v := os.Getenv("RG_ID"); v != "" { req.Header.Set("x-acs-resourcegroupid", v) }
	}

	tr := &http.Transport{
		TLSClientConfig:   &tls.Config{InsecureSkipVerify: insecure},
		ForceAttemptHTTP2: false,
		// Force HTTP/1.1 over the tunnel (disable h2), matching curl.
		TLSNextProto: map[string]func(string, *tls.Conn) http.RoundTripper{},
	}
	if proxy != nil { tr.Proxy = http.ProxyURL(proxy) }
	if os.Getenv("APSARA_RPC_DEBUG") == "1" {
		fmt.Fprintf(os.Stderr, "[apsara-rpc] NATIVE_SEND %s proxy=%v insecure=%v\n", target, proxy, insecure)
	}
	resp, err := (&http.Client{Transport: tr}).Do(req)
	if err != nil { die("native send:", err) }
	defer resp.Body.Close()
	if os.Getenv("APSARA_RPC_DEBUG") == "1" {
		fmt.Fprintf(os.Stderr, "[apsara-rpc] resp proto=%s status=%s tls=%v\n", resp.Proto, resp.Status, resp.TLS != nil)
	}
	b, _ := io.ReadAll(resp.Body)
	fmt.Println(string(b))
	if resp.StatusCode >= 300 { os.Exit(1) } // surface API errors to run_cli
}
