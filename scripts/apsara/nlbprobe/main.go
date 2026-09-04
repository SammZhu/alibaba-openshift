// nlbprobe — can the CCM reach NLB here, and can it parse what comes back?
//
// Why this exists: SLB is a dead end for now.  ccmprobe showed this gateway
// answers DescribeLoadBalancers with HTTP 200 and real data, with or without
// the tenancy headers — but the response carries LoadBalancer.Tags as a bare
// array where aliyun/alibaba-cloud-sdk-go v1.63.99 (the version the CCM
// embeds) declares an object, so it fails client-side.  Fixing that means
// forking the SDK, and Tags may not be the only field that differs.
//
// The CCM talks to NLB through an entirely different stack —
// alibabacloud-go/nlb-20220430 on darabonba-openapi/tea — with its own
// serialisation.  If NLB is reachable AND parses, LoadBalancer Services can go
// through NLB and no SDK fork is needed at all.  That is worth ten minutes
// before starting one.
//
//	cd scripts/apsara/nlbprobe && go mod tidy && go build -o nlbprobe .
//	NLB_ENDPOINT=... REGION=... AK=... SK=... ./nlbprobe
//
// Read-only: ListLoadBalancers creates nothing.  An empty list is a PASS — the
// call is what is under test, not the contents.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	openapi "github.com/alibabacloud-go/darabonba-openapi/v2/client"
	nlb "github.com/alibabacloud-go/nlb-20220430/v4/client"
	util "github.com/alibabacloud-go/tea-utils/v2/service"
	"github.com/alibabacloud-go/tea/tea"
)

func main() {
	for _, k := range []string{"REGION", "NLB_ENDPOINT", "AK", "SK"} {
		if os.Getenv(k) == "" {
			fmt.Printf("missing required env %s\n", k)
			os.Exit(2)
		}
	}
	region, ep := os.Getenv("REGION"), os.Getenv("NLB_ENDPOINT")
	fmt.Printf("endpoint = %s   region = %s   protocol = HTTP\n\n", ep, region)

	client, err := nlb.NewClient(&openapi.Config{
		AccessKeyId:     tea.String(os.Getenv("AK")),
		AccessKeySecret: tea.String(os.Getenv("SK")),
		Endpoint:        tea.String(ep),
		// Same reason as everywhere else here: the gateway's certificate is
		// signed by the environment's own CA.
		Protocol: tea.String("HTTP"),
		RegionId: tea.String(region),
	})
	if err != nil {
		fmt.Printf("client FAILED: %v\n", err)
		os.Exit(1)
	}

	resp, err := client.ListLoadBalancersWithOptions(
		&nlb.ListLoadBalancersRequest{RegionId: tea.String(region)},
		&util.RuntimeOptions{},
	)
	if err != nil {
		fmt.Printf("ERROR: %v\n", err)
		// A tea SDKError carries the server's own body — print it, because the
		// interesting distinction is "gateway refused" vs "client could not
		// parse a perfectly good answer".
		if sdkErr, ok := err.(*tea.SDKError); ok {
			fmt.Printf("  code    = %s\n", tea.StringValue(sdkErr.Code))
			fmt.Printf("  message = %s\n", tea.StringValue(sdkErr.Message))
			if sdkErr.Data != nil {
				b, _ := json.Marshal(sdkErr.Data)
				s := string(b)
				if len(s) > 600 {
					s = s[:600] + " …"
				}
				fmt.Printf("  data    = %s\n", s)
			}
		}
		fmt.Println("\nreading: a refusal means NLB is not usable here — fall back to forking")
		fmt.Println("         the SLB SDK.  A parse error means the same shape problem")
		fmt.Println("         followed us and NLB buys nothing.")
		os.Exit(1)
	}

	b, _ := json.Marshal(resp.Body)
	s := string(b)
	if len(s) > 800 {
		s = s[:800] + " …"
	}
	fmt.Printf("OK  RequestId=%s  TotalCount=%v  returned=%d\n",
		tea.StringValue(resp.Body.RequestId),
		tea.Int32Value(resp.Body.TotalCount),
		len(resp.Body.LoadBalancers))
	fmt.Printf("body: %s\n", s)
	fmt.Println("\nreading: OK -> LoadBalancer Services can go through NLB; no SDK fork needed.")
}
