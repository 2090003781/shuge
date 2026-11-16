// 账本校验工具：一条命令回答"到底丢没丢"。
//
// 用法：
//
//	go run ./cmd/ledgercheck
//	go run ./cmd/ledgercheck --dir data/ledger --json
//
// 判据：seq 单调 +1 连续（缺口即异常）；坏行单独列出；轮转边界衔接；末尾半行告警。
// 退出码：0 = 无缺口；1 = 有缺口/坏行；2 = 读取失败。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"core/internal/ledger"
)

func main() {
	dir := flag.String("dir", "data/ledger", "账本目录")
	asJSON := flag.Bool("json", false, "以 JSON 输出")
	detail := flag.Bool("detail", false, "同时校验分账（data/ledger/detail/<owner>/）")
	flag.Parse()

	rep, err := ledger.Verify(*dir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "账本校验失败: %v\n", err)
		os.Exit(2)
	}

	if *asJSON {
		b, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(b))
	} else {
		fmt.Printf("账本目录：%s\n", rep.Dir)
		for _, f := range rep.Files {
			mark := ""
			if f.TailHalf {
				mark = "   ⚠ 末尾半行"
			}
			if f.BadLines > 0 {
				mark += fmt.Sprintf("   ⚠ 坏行 %d", f.BadLines)
			}
			fmt.Printf("  %-36s %8.1f KB %6d 行  seq %d→%d%s\n",
				f.Name, float64(f.Bytes)/1024, f.Lines, f.FirstSeq, f.LastSeq, mark)
		}
		if len(rep.Gaps) > 0 {
			fmt.Println("⚠ 存在序号缺口：")
			for _, g := range rep.Gaps {
				fmt.Printf("   seq %d → %d（缺 %d 条）\n", g.AfterSeq, g.BeforeSeq, g.Missing)
			}
		}
		if len(rep.BadLines) > 0 {
			fmt.Println("⚠ 存在坏行：")
			for _, b := range rep.BadLines {
				fmt.Printf("   %s:%d %s\n", b.File, b.Line, b.Err)
			}
		}
		fmt.Println(rep.Summary)
	}

	if *detail {
		reports, derr := ledger.VerifyDetail(*dir)
		if derr != nil {
			fmt.Fprintf(os.Stderr, "分账校验失败: %v\n", derr)
			os.Exit(2)
		}
		if len(reports) == 0 {
			fmt.Println("（暂无分账：data/ledger/detail/ 下没有 owner 目录）")
		}
		bad := false
		for _, r := range reports {
			status := "OK  "
			if !r.OK {
				status = "异常"
				bad = true
			}
			fmt.Printf("  [%s] owner=%-26s %6d 行  seq %d→%d  缺口 %d  坏行 %d  文件 %d\n",
				status, r.Owner, r.Lines, r.FirstSeq, r.LastSeq, len(r.Gaps), r.BadLines, len(r.Files))
			for _, f := range r.Files {
				mark := ""
				if f.TailHalf {
					mark = "   ⚠ 末尾半行"
				}
				fmt.Printf("        %-42s %8.1f KB %6d 行 seq %d→%d%s\n",
					f.Name, float64(f.Bytes)/1024, f.Lines, f.FirstSeq, f.LastSeq, mark)
			}
		}
		if bad {
			os.Exit(1)
		}
	}

	if !rep.OK {
		os.Exit(1)
	}
}
