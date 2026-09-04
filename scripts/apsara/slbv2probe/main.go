// slbv2probe — does the V2 SDK parse what the V1 SDK chokes on?
//
// Context: the CCM reaches SLB through aliyun/alibaba-cloud-sdk-go (V1), and on
// this gateway that call returns HTTP 200 with a body the V1 structs cannot
// decode — LoadBalancer.Tags arrives as a bare array where V1 declares an
// object.  shapecheck measured the damage at exactly one field, so patching V1
// is small.  V1 is also archived upstream (EOL 2025-03-01), which raises the
// fair question of whether to move to the V2 SDK instead.
//
// Moving is NOT a dependency bump — the CCM's whole SLB layer is written
// against V1, so it would be a rewrite, against one UnmarshalJSON for the patch.
// This probe exists to price the fallback, not to take it: if the V1 patch later
// grows past one or two fields, we want to already know whether V2 handles this
// gateway cleanly, rather than finding out then.
//
//	SLB_ENDPOINT=slb-vpc.cloud.dyz7.com REGION=... AK=... SK=... ./slbv2probe
//
// Read-only.  An empty list is a PASS: the call is what is under test.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	openapi "github.com/alibabacloud-go/darabonba-openapi/v2/client"
	slb "github.com/alibabacloud-go/slb-20140515/v4/client"
	util "github.com/alibabacloud-go/tea-utils/v2/service"
	"github.com/alibabacloud-go/tea/tea"
)

func main() {
	for _, k := range []string{"REGION", "SLB_ENDPOINT", "AK", "SK"} {
		if os.Getenv(k) == "" {
			fmt.Printf("missing required env %s\n", k)
			os.Exit(2)
		}
	}
	region, ep := os.Getenv("REGION"), os.Getenv("SLB_ENDPOINT")
	fmt.Printf("endpoint = %s   region = %s   protocol = HTTP   sdk = V2 (slb-20140515/v4)\n\n", ep, region)

	client, err := slb.NewClient(&openapi.Config{
		AccessKeyId:     tea.String(os.Getenv("AK")),
		AccessKeySecret: tea.String(os.Getenv("SK")),
		Endpoint:        tea.String(ep),
		Protocol:        tea.String("HTTP"), // gateway cert is signed by the environment's own CA
		RegionId:        tea.String(region),
	})
	if err != nil {
		fmt.Printf("client FAILED: %v\n", err)
		os.Exit(1)
	}

	resp, err := client.DescribeLoadBalancersWithOptions(
		&slb.DescribeLoadBalancersRequest{RegionId: tea.String(region)},
		&util.RuntimeOptions{},
	)
	if err != nil {
		fmt.Printf("ERROR: %v\n", err)
		if sdkErr, ok := err.(*tea.SDKError); ok {
			fmt.Printf("  code    = %s\n", tea.StringValue(sdkErr.Code))
			fmt.Printf("  message = %s\n", tea.StringValue(sdkErr.Message))
		}
		fmt.Println("\nreading: V2 fails too -> it is no fallback; patch V1 and stop considering it.")
		os.Exit(1)
	}

	n := 0
	if resp.Body != nil && resp.Body.LoadBalancers != nil {
		n = len(resp.Body.LoadBalancers.LoadBalancer)
	}
	fmt.Printf("OK  RequestId=%s  TotalCount=%v  returned=%d\n",
		tea.StringValue(resp.Body.RequestId), tea.Int32Value(resp.Body.TotalCount), n)

	// Print one entry's Tags specifically: that is the field V1 cannot decode, so
	// "V2 returned something" is not the answer — "V2 decoded THAT field" is.
	if n > 0 {
		b, _ := json.Marshal(resp.Body.LoadBalancers.LoadBalancer[0])
		s := string(b)
		if len(s) > 700 {
			s = s[:700] + " …"
		}
		fmt.Printf("first entry: %s\n", s)
	}
	fmt.Println("\nreading: OK with Tags populated -> V2 is a real fallback if the V1 patch grows.")
	fmt.Println("         OK but Tags empty/absent -> V2 tolerated the shape by dropping it;")
	fmt.Println("         fine for the CCM (it does not use Tags) but say so rather than")
	fmt.Println("         calling it a clean parse.")
}
