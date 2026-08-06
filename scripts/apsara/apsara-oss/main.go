// apsara-oss: a self-contained OSS client covering the object operations the
// ansible layer uses, standing in for `aliyun oss <cmd>` (ossutil) when the
// target is Apsara Stack — so the operator host does not need the aliyun CLI /
// ossutil installed.  OSS is the data plane (not the RPC gateway), so this is a
// separate tool from apsara-rpc.
//
// Usage (flags mirror `aliyun oss`; each may also come from the environment):
//   apsara-oss cp   <src> <dst>            # upload (local->oss), download (oss->local)
//   apsara-oss rm   <oss://b/key> [--recursive]
//   apsara-oss ls   <oss://b/prefix>
//   apsara-oss stat <oss://b/key>
//
// Flags / env:
//   --endpoint=          OSS_ENDPOINT
//   --access-key-id=     AK
//   --access-key-secret= SK
//   --sts-token=         OSS_STS_TOKEN
//   --recursive          (rm)                 -f / --force  (accepted, no-op)
//   --region=            (accepted; endpoint carries the region)
package main

import (
	"fmt"
	"os"
	"strings"

	"github.com/aliyun/aliyun-oss-go-sdk/oss"
)

func die(a ...interface{}) { fmt.Fprintln(os.Stderr, a...); os.Exit(1) }

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

// parseArgs splits argv into positionals and --flag[=value] / bare flags.
func parseArgs(args []string) (pos []string, flags map[string]string, bools map[string]bool) {
	flags = map[string]string{}
	bools = map[string]bool{}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if !strings.HasPrefix(a, "-") {
			pos = append(pos, a)
			continue
		}
		a = strings.TrimLeft(a, "-")
		if strings.Contains(a, "=") {
			kv := strings.SplitN(a, "=", 2)
			flags[kv[0]] = kv[1]
			continue
		}
		// bare flag, or space-separated value for the known valued flags
		switch a {
		case "recursive", "r", "f", "force":
			bools[a] = true
		default:
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				flags[a] = args[i+1]
				i++
			} else {
				bools[a] = true
			}
		}
	}
	return
}

// splitOSS parses oss://bucket/key into (bucket, key).
func splitOSS(uri string) (bucket, key string, ok bool) {
	if !strings.HasPrefix(uri, "oss://") {
		return "", "", false
	}
	rest := strings.TrimPrefix(uri, "oss://")
	parts := strings.SplitN(rest, "/", 2)
	bucket = parts[0]
	if len(parts) == 2 {
		key = parts[1]
	}
	return bucket, key, true
}

func newBucket(flags map[string]string, bucket string) *oss.Bucket {
	endpoint := flags["endpoint"]
	if endpoint == "" {
		endpoint = os.Getenv("OSS_ENDPOINT")
	}
	ak := env("AK", flags["access-key-id"])
	if v := flags["access-key-id"]; v != "" {
		ak = v
	}
	sk := env("SK", flags["access-key-secret"])
	if v := flags["access-key-secret"]; v != "" {
		sk = v
	}
	token := flags["sts-token"]
	if token == "" {
		token = os.Getenv("OSS_STS_TOKEN")
	}
	if endpoint == "" || ak == "" || sk == "" {
		die("apsara-oss: need endpoint (--endpoint/OSS_ENDPOINT) + AK/SK (--access-key-id/-secret or AK/SK env)")
	}
	var opts []oss.ClientOption
	if token != "" {
		opts = append(opts, oss.SecurityToken(token))
	}
	client, err := oss.New(endpoint, ak, sk, opts...)
	if err != nil {
		die("oss client:", err)
	}
	b, err := client.Bucket(bucket)
	if err != nil {
		die("oss bucket:", err)
	}
	return b
}

func main() {
	if len(os.Args) < 3 {
		die("usage: apsara-oss <cp|rm|ls|stat> <args> [--endpoint= --access-key-id= --access-key-secret= ...]")
	}
	cmd := os.Args[1]
	pos, flags, bools := parseArgs(os.Args[2:])

	switch cmd {
	case "cp":
		if len(pos) < 2 {
			die("cp: need <src> <dst>")
		}
		src, dst := pos[0], pos[1]
		sb, sk, srcIsOSS := splitOSS(src)
		db, dk, dstIsOSS := splitOSS(dst)
		switch {
		case !srcIsOSS && dstIsOSS: // upload
			if err := newBucket(flags, db).PutObjectFromFile(dk, src); err != nil {
				die("upload:", err)
			}
		case srcIsOSS && !dstIsOSS: // download
			if err := newBucket(flags, sb).GetObjectToFile(sk, dst); err != nil {
				die("download:", err)
			}
		default:
			die("cp: exactly one of <src>/<dst> must be an oss:// URI (oss<->oss not supported)")
		}
	case "rm":
		if len(pos) < 1 {
			die("rm: need <oss://bucket/key>")
		}
		b, key, ok := splitOSS(pos[0])
		if !ok {
			die("rm: target must be an oss:// URI")
		}
		bkt := newBucket(flags, b)
		if bools["recursive"] || bools["r"] {
			marker := ""
			for {
				res, err := bkt.ListObjects(oss.Prefix(key), oss.Marker(marker), oss.MaxKeys(1000))
				if err != nil {
					die("list for rm:", err)
				}
				keys := make([]string, 0, len(res.Objects))
				for _, o := range res.Objects {
					keys = append(keys, o.Key)
				}
				if len(keys) > 0 {
					if _, err := bkt.DeleteObjects(keys); err != nil {
						die("delete:", err)
					}
				}
				if !res.IsTruncated {
					break
				}
				marker = res.NextMarker
			}
		} else {
			if err := bkt.DeleteObject(key); err != nil {
				die("delete:", err)
			}
		}
	case "ls":
		if len(pos) < 1 {
			die("ls: need <oss://bucket/prefix>")
		}
		b, prefix, ok := splitOSS(pos[0])
		if !ok {
			die("ls: target must be an oss:// URI")
		}
		bkt := newBucket(flags, b)
		marker := ""
		for {
			res, err := bkt.ListObjects(oss.Prefix(prefix), oss.Marker(marker), oss.MaxKeys(1000))
			if err != nil {
				die("list:", err)
			}
			for _, o := range res.Objects {
				fmt.Printf("%12d  %s  oss://%s/%s\n", o.Size, o.LastModified.Format("2006-01-02 15:04:05"), b, o.Key)
			}
			if !res.IsTruncated {
				break
			}
			marker = res.NextMarker
		}
	case "stat":
		if len(pos) < 1 {
			die("stat: need <oss://bucket/key>")
		}
		b, key, ok := splitOSS(pos[0])
		if !ok {
			die("stat: target must be an oss:// URI")
		}
		exist, err := newBucket(flags, b).IsObjectExist(key)
		if err != nil {
			die("stat:", err)
		}
		if !exist {
			die("stat: object does not exist: " + pos[0])
		}
		fmt.Printf("oss://%s/%s exists\n", b, key)
	default:
		die("unknown command: " + cmd + " (want cp|rm|ls|stat)")
	}
}
