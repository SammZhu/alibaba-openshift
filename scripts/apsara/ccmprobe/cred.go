package main

import "github.com/aliyun/alibaba-cloud-sdk-go/sdk/auth/credentials"

// Static AK/SK.  The CCM also accepts these (ALIBABA_CLOUD_ACCESS_KEY_ID /
// _SECRET are in its binary), so a probe that passes here maps directly onto a
// configuration the CCM can actually be given.
func newCredential(ak, sk string) *credentials.BaseCredential {
	return &credentials.BaseCredential{AccessKeyId: ak, AccessKeySecret: sk}
}
