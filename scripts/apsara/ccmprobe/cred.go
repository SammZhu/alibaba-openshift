package main

import "github.com/aliyun/alibaba-cloud-sdk-go/sdk/auth/credentials"

// Static AK/SK.  The CCM accepts these too (ALIBABA_CLOUD_ACCESS_KEY_ID /
// _SECRET are in its binary), so a probe that passes here maps onto a
// configuration the CCM can actually be handed.
//
// AccessKeyCredential, not BaseCredential: v1.63.99 — the version the CCM
// embeds — rejects BaseCredential outright with SDK.UnsupportedCredential,
// while v1.62.676 accepted it.  Probing with the wrong type answers nothing
// about the environment, which is what the first v1.63.99 run did.
func newCredential(ak, sk string) *credentials.AccessKeyCredential {
	return credentials.NewAccessKeyCredential(ak, sk)
}
