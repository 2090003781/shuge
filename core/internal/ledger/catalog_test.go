// 本文件：事件目录的"漏登记"回归测试（2026-09-13 加）。
//
// 为什么要有它：目录的唯一价值是"漏登记可见"，而漏登记的典型形态是
// **生产者写了新事件类型、忘了登记**。只测目录自己（比如"这三个事件登记了吗"）
// 抓不到这种错——必须去**生产者源码**里把 `platform.*` 字面量挖出来跟目录对照。
//
// 写这个测试的当天就抓到一处真漏登记：`platform.patrol.*`（主进程常驻巡检）。
// 它只在"结论跃迁"时才写账，一直没跃迁过 → 账本里 0 条 unknown → 漏登记被完美掩盖；
// 等第一次真出问题时，这条最该被看见的记录反而会被记成"未登记的平台事件"。
package ledger

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// platformTypeRe 匹配源码里的平台事件名字面量。
// 末尾的 `\.?` 是为了兜住拼接写法：`"platform.patrol." + kind`（go/patrol.go 就是这么写的）。
var platformTypeRe = regexp.MustCompile(`platform\.[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*\.?`)

// strLitRe 抓字符串字面量（Go 双引号 / Python 单双引号都算），注释一律不看
var strLitRe = regexp.MustCompile(`"([^"\n]*)"|'([^'\n]*)'`)

// TestEveryProducedPlatformEventIsRegistered 扫全仓 .go/.py，逐个核对登记表
func TestEveryProducedPlatformEventIsRegistered(t *testing.T) {
	root := filepath.Join("..", "..")
	var files []string
	err := filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return nil // 权限/竞态问题不阻断扫描
		}
		if info.IsDir() {
			switch info.Name() {
			case ".git", "bin", "data", "node_modules", ".trash":
				return filepath.SkipDir
			}
			return nil
		}
		// _test.go 里的 platform.* 是测试夹具（故意造的类型），不是生产者
		if strings.HasSuffix(p, "_test.go") {
			return nil
		}
		if strings.HasSuffix(p, ".go") || strings.HasSuffix(p, ".py") {
			files = append(files, p)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(files) < 3 {
		t.Fatalf("只扫到 %d 个源文件，测试本身失效（路径基准错了？）", len(files))
	}

	missing := map[string]string{} // 事件名 → 出处
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		// 只看**字符串字面量**里的类型名：注释里提到事件名（解释、举例）不该算"生产"，
		// 否则一提名字就被要求登记，测试会变成噪声源。
		rel := filepath.ToSlash(f)
		for _, sm := range strLitRe.FindAllStringSubmatch(string(b), -1) {
			lit := sm[1]
			if lit == "" {
				lit = sm[2]
			}
			for _, m := range platformTypeRe.FindAllString(lit, -1) {
				if strings.HasSuffix(m, ".") {
					// 前缀拼接写法（"platform.patrol." + kind）：要求目录里至少有一条以它开头
					if !hasCatalogPrefix(m) {
						missing[m+"*"] = rel
					}
					continue
				}
				if _, ok := Catalog[m]; !ok {
					missing[m] = rel
				}
			}
		}
	}
	if len(missing) > 0 {
		keys := make([]string, 0, len(missing))
		for k := range missing {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			t.Errorf("平台事件未登记：%s（出现在 %s）→ 去 internal/ledger/catalog.go 登记，"+
				"否则账本会把它当 unknown（域对得上、档位按默认），漏登记就白统计了", k, missing[k])
		}
	}
}

func hasCatalogPrefix(prefix string) bool {
	for k := range Catalog {
		if strings.HasPrefix(k, prefix) {
			return true
		}
	}
	return false
}

// TestCatalogEntriesAreWellFormed 目录条目自检：平台域必须 platform. 开头等
func TestCatalogEntriesAreWellFormed(t *testing.T) {
	validRealm := map[string]bool{
		RealmPlatform: true, RealmAgent: true, RealmPlugin: true,
		RealmVault: true, RealmExternal: true,
	}
	for name, spec := range Catalog {
		if name == "" || !strings.Contains(name, ".") {
			t.Errorf("事件名不像点分机器名：%q", name)
		}
		if strings.HasPrefix(name, PlatformPrefix) && spec.Realm != RealmPlatform {
			t.Errorf("%s 以 %s 开头，域却是 %s（域与名字必须一致，否则筛选会串）",
				name, PlatformPrefix, spec.Realm)
		}
		if spec.Realm != RealmPlatform && strings.HasPrefix(name, PlatformPrefix) {
			t.Errorf("%s 域不对", name)
		}
		if !validRealm[spec.Realm] {
			t.Errorf("%s 的域是未知值：%q", name, spec.Realm)
		}
		switch spec.Class {
		case ClassCritical, ClassBusiness, ClassTelemetry:
		default:
			t.Errorf("%s 的档位是未知值：%q", name, spec.Class)
		}
		if strings.TrimSpace(spec.Desc) == "" {
			t.Errorf("%s 没有中文展示名（控制台会显示空白）", name)
		}
	}
}

// TestClassifyFallsBackHonestly 未登记的类型：platform. 前缀仍算平台域（但 known=false），
// 其余算外部域——这两条是"漏登记可见"和"外部噪声不入账"的分界，不能被悄悄改掉。
func TestClassifyFallsBackHonestly(t *testing.T) {
	spec, known := Classify("platform.不存在的子系统.动作")
	if known {
		t.Fatal("未登记的类型不能算 known")
	}
	if spec.Realm != RealmPlatform {
		t.Fatalf("platform. 前缀应仍算平台域，实际 %q", spec.Realm)
	}

	spec, known = Classify("dsh.user.message")
	if known || spec.Realm != RealmExternal {
		t.Fatalf("非 platform. 前缀应算外部域且 known=false，实际 %+v known=%v", spec, known)
	}

	// 上下文注入链路三段（本次新增）必须都在册且是平台域
	for _, name := range []string{
		"platform.context.pack-ready",
		"platform.context.pack-changed",
		"platform.context.injected",
		"platform.context.inject-missed",
		"platform.patrol.problem",
		"platform.patrol.recovered",
		"platform.patrol.changed",
	} {
		spec, known := Classify(name)
		if !known || spec.Realm != RealmPlatform {
			t.Errorf("%s 应已登记且属平台域，实际 known=%v realm=%q", name, known, spec.Realm)
		}
	}
}
