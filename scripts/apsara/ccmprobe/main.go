// ccmprobe — does the Alibaba CCM's API path work on this Apsara environment?
//
// The CCM (v2.x) can be pointed at private-cloud endpoints with ECS_ENDPOINT /
// VPC_ENDPOINT / SLB_ENDPOINT / NLB_ENDPOINT and told to speak HTTP with
// ALICLOUD_CLIENT_SCHEME.  What it has NO knob for is the tenancy headers
// (x-acs-organizationid / -resourcegroupid / -regionid) that apsara-rpc, the
// CAPA provider and the CSI driver all inject.
//
// So the question that decides whether CCM needs a code fork is exactly one:
//
//	does this gateway accept a signed request WITHOUT the tenancy headers?
//
// This probe answers it as a controlled comparison — the same call, twice, the
// only difference being the headers.  Guessing from a single run is how three
// earlier hypotheses in this project survived longer than they deserved.
//
// Build + run on the operator host:
//
//	cd scripts/apsara/ccmprobe && go mod tidy && go build -o ccmprobe .
//	SLB_ENDPOINT=slb-vpc.cloud.dyz7.com \
//	REGION=cn-wulan-dyz7-d01 \
//	AK=... SK=... ORG_ID=... RG_ID=... \
//	./ccmprobe
//
// Nothing is created or deleted: DescribeLoadBalancers is read-only, and an
// empty result is a perfectly good PASS — we are testing the call, not the
// contents.
package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/aliyun/alibaba-cloud-sdk-go/sdk"
	"github.com/aliyun/alibaba-cloud-sdk-go/sdk/endpoints"
	"github.com/aliyun/alibaba-cloud-sdk-go/services/slb"
)

// headerInjector adds the tenancy headers beneath the signing layer.  These
// headers do not participate in ACS v1 signing (only the query string is
// signed), so adding them here cannot invalidate the signature.
//
// Deliberately NOT an *http.Transport: the SDK type-asserts its transport to
// *http.Transport when it applies timeouts and writes the result back, which
// would silently discard a wrapper that satisfied the assertion.
type headerInjector struct {
	inner   http.RoundTripper
	headers map[string]string
	dump    *string
}

func (h *headerInjector) RoundTrip(req *http.Request) (*http.Response, error) {
	for k, v := range h.headers {
		if v != "" {
			req.Header.Set(k, v)
		}
	}
	resp, err := h.inner.RoundTrip(req)
	if err != nil || resp == nil || h.dump == nil {
		return resp, err
	}
	body, rerr := io.ReadAll(resp.Body)
	resp.Body.Close()
	if rerr == nil {
		*h.dump = string(body)
		resp.Body = io.NopCloser(strings.NewReader(string(body)))
	}
	return resp, err
}

func describe(label string, withHeaders bool) {
	region := os.Getenv("REGION")
	ep := os.Getenv("SLB_ENDPOINT")

	headers := map[string]string{}
	if withHeaders {
		headers["x-acs-organizationid"] = os.Getenv("ORG_ID")
		headers["x-acs-resourcegroupid"] = os.Getenv("RG_ID")
		headers["x-acs-regionid"] = region
	}
	var raw string

	cfg := sdk.NewConfig()
	cfg.Scheme = "HTTP" // the gateway's cert is signed by the environment's own CA
	cfg.Transport = &headerInjector{inner: http.DefaultTransport, headers: headers, dump: &raw}

	if err := endpoints.AddEndpointMapping(region, "Slb", ep); err != nil {
		fmt.Printf("%-18s  endpoint mapping FAILED: %v\n", label, err)
		return
	}

	client, err := slb.NewClientWithOptions(region, cfg,
		newCredential(os.Getenv("AK"), os.Getenv("SK")))
	if err != nil {
		fmt.Printf("%-18s  client FAILED: %v\n", label, err)
		return
	}

	req := slb.CreateDescribeLoadBalancersRequest()
	req.RegionId = region
	req.Scheme = "HTTP"

	resp, err := client.DescribeLoadBalancers(req)
	switch {
	case err != nil:
		fmt.Printf("%-18s  ERROR: %v\n", label, err)
	default:
		fmt.Printf("%-18s  OK  RequestId=%q TotalCount=%d LoadBalancers=%d\n",
			label, resp.RequestId, resp.TotalCount, len(resp.LoadBalancers.LoadBalancer))
	}
	if raw == "" {
		return
	}

	// Write the WHOLE body out, not a preview.  The open question is no longer
	// "does this call work" (it does) but "how many fields does this gateway
	// shape differently from the SDK's structs" — Tags is one, and one is only
	// a floor.  With the full body on disk that can be answered offline, in a
	// single pass, instead of one network round trip per field discovered.
	if dir := os.Getenv("DUMP_DIR"); dir != "" {
		name := filepath.Join(dir, "slb-describe-"+strings.ReplaceAll(label, " ", "_")+".json")
		if werr := os.WriteFile(name, []byte(raw), 0o644); werr != nil {
			fmt.Printf("%-18s  dump FAILED: %v\n", "", werr)
		} else {
			fmt.Printf("%-18s  raw body -> %s (%d bytes)\n", "", name, len(raw))
		}
	}
	preview := raw
	if len(preview) > 400 {
		preview = preview[:400] + " …"
	}
	fmt.Printf("%-18s  raw: %s\n", "", preview)
}

func main() {
	// Offline mode: analyse a body captured by an earlier run.  Needs no
	// network, no credentials, and can be re-run as often as the question
	// changes.
	if len(os.Args) > 2 && os.Args[1] == "shapecheck" {
		os.Exit(shapecheck(os.Args[2], slb.CreateDescribeLoadBalancersResponse()))
	}

	for _, k := range []string{"REGION", "SLB_ENDPOINT", "AK", "SK"} {
		if os.Getenv(k) == "" {
			fmt.Printf("missing required env %s\n", k)
			os.Exit(2)
		}
	}
	fmt.Printf("endpoint = %s   region = %s   scheme = HTTP\n\n",
		os.Getenv("SLB_ENDPOINT"), os.Getenv("REGION"))

	// The control pair.  If both succeed, the CCM needs no code change at all.
	// If only "with headers" succeeds, the tenancy headers are mandatory and the
	// CCM needs the same RoundTripper the CAPA provider carries.
	describe("with headers", true)
	fmt.Println()
	describe("WITHOUT headers", false)

	fmt.Println("\nreading — compare the two runs, and read the raw body, not just the error:")
	fmt.Println("  both OK                     -> CCM works on configuration alone (env vars only).")
	fmt.Println("  only 'with headers' OK      -> tenancy headers are mandatory; CCM needs a forked")
	fmt.Println("                                 client carrying the RoundTripper, as CAPA does.")
	fmt.Println("  both show JsonUnmarshalError with a 200 body")
	fmt.Println("                              -> the CALL is fine and the headers are NOT needed;")
	fmt.Println("                                 the SDK's structs disagree with what this gateway")
	fmt.Println("                                 returns.  A client-side parse failure, not a")
	fmt.Println("                                 rejection — re-probe with the SDK version the CCM")
	fmt.Println("                                 actually embeds before concluding anything.")
	fmt.Println("  both fail with no body      -> endpoint or credentials wrong; fix that first.")
}
