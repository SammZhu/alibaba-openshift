package main

// shapecheck — walk a captured response body against the SDK's declared structs
// and report EVERY shape disagreement in one pass.
//
//	./ccmprobe shapecheck /home/claude/slb-describe-with_headers.json
//
// The point is to size the problem before deciding to fork the SDK.  Hitting
// one mismatch, patching it, and re-running discovers the next one a network
// round trip later; this answers "one field or fifteen?" immediately.
//
// Two categories are reported separately, because they have different costs:
//
//   MISMATCH  — object where an array is declared, or the reverse.  jsoniter's
//               fuzzy decoder cannot bridge this; it is what breaks the call.
//   tolerated — number where a string is declared, and similar.  The SDK
//               registers newBetterFuzzyExtension, which converts these
//               silently.  Listed only so they are not mistaken for problems:
//               an earlier investigation lost time blaming exactly this
//               category for a failure it did not cause.

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"sort"
	"strings"
)

type finding struct {
	path, detail string
	hard         bool
}

func jsonName(f reflect.StructField) string {
	tag := f.Tag.Get("json")
	if tag == "" {
		return f.Name
	}
	if i := strings.Index(tag, ","); i >= 0 {
		tag = tag[:i]
	}
	if tag == "" || tag == "-" {
		return f.Name
	}
	return tag
}

func kindOf(v interface{}) string {
	switch v.(type) {
	case map[string]interface{}:
		return "object"
	case []interface{}:
		return "array"
	case string:
		return "string"
	case float64:
		return "number"
	case bool:
		return "bool"
	case nil:
		return "null"
	}
	return "?"
}

func walk(path string, t reflect.Type, v interface{}, out *[]finding) {
	if v == nil {
		return
	}
	for t.Kind() == reflect.Ptr {
		t = t.Elem()
	}

	switch t.Kind() {
	case reflect.Struct:
		m, ok := v.(map[string]interface{})
		if !ok {
			*out = append(*out, finding{path, fmt.Sprintf("declared object (%s), got %s", t.Name(), kindOf(v)), true})
			return
		}
		byName := map[string]reflect.StructField{}
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			byName[strings.ToLower(jsonName(f))] = f
		}
		for k, sub := range m {
			f, known := byName[strings.ToLower(k)]
			if !known {
				continue // extra fields are harmless; the decoder ignores them
			}
			walk(path+"."+k, f.Type, sub, out)
		}

	case reflect.Slice, reflect.Array:
		arr, ok := v.([]interface{})
		if !ok {
			*out = append(*out, finding{path, fmt.Sprintf("declared array, got %s", kindOf(v)), true})
			return
		}
		for i, e := range arr {
			walk(fmt.Sprintf("%s[%d]", path, i), t.Elem(), e, out)
		}

	case reflect.String:
		if k := kindOf(v); k != "string" && k != "null" {
			*out = append(*out, finding{path, fmt.Sprintf("declared string, got %s", k), k == "object" || k == "array"})
		}

	case reflect.Bool:
		if k := kindOf(v); k != "bool" && k != "null" {
			*out = append(*out, finding{path, fmt.Sprintf("declared bool, got %s", k), k == "object" || k == "array"})
		}

	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64,
		reflect.Float32, reflect.Float64:
		if k := kindOf(v); k != "number" && k != "null" {
			*out = append(*out, finding{path, fmt.Sprintf("declared %s, got %s", t.Kind(), k), k == "object" || k == "array"})
		}
	}
}

func shapecheck(file string, target interface{}) int {
	body, err := os.ReadFile(file)
	if err != nil {
		fmt.Printf("cannot read %s: %v\n", file, err)
		return 2
	}
	var generic interface{}
	if err := json.Unmarshal(body, &generic); err != nil {
		fmt.Printf("%s is not JSON: %v\n", file, err)
		return 2
	}

	var out []finding
	walk("$", reflect.TypeOf(target), generic, &out)
	sort.Slice(out, func(i, j int) bool {
		if out[i].hard != out[j].hard {
			return out[i].hard
		}
		return out[i].path < out[j].path
	})

	hard := 0
	for _, f := range out {
		if f.hard {
			hard++
		}
	}
	fmt.Printf("%s\n%d byte(s) of body, %d disagreement(s): %d MISMATCH, %d tolerated\n\n",
		file, len(body), len(out), hard, len(out)-hard)

	// Collapse the array indices: "…LoadBalancer[0].Tags" and "[7].Tags" are one
	// field to fix, not eight.  Reporting them separately would overstate the
	// cost, which is the very thing this tool exists to measure.
	seen := map[string]int{}
	var order []string
	for _, f := range out {
		key := fmt.Sprintf("%-9s %s  — %s", map[bool]string{true: "MISMATCH", false: "tolerated"}[f.hard],
			collapse(f.path), f.detail)
		if seen[key] == 0 {
			order = append(order, key)
		}
		seen[key]++
	}
	for _, k := range order {
		if seen[k] > 1 {
			fmt.Printf("  %s  (×%d)\n", k, seen[k])
		} else {
			fmt.Printf("  %s\n", k)
		}
	}
	if hard == 0 {
		fmt.Println("\nno hard mismatches: this body would decode.")
	} else {
		fmt.Printf("\n%d distinct field(s) need fixing in a fork.\n", countDistinctHard(order))
	}
	return 0
}

func collapse(p string) string {
	var b strings.Builder
	skip := false
	for _, r := range p {
		switch {
		case r == '[':
			skip = true
			b.WriteString("[]")
		case r == ']':
			skip = false
		case !skip:
			b.WriteRune(r)
		}
	}
	return b.String()
}

func countDistinctHard(order []string) int {
	n := 0
	for _, k := range order {
		if strings.HasPrefix(k, "MISMATCH") {
			n++
		}
	}
	return n
}
