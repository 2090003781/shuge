// 本文件：分账（detail ledger）——总账记「结论 + 指针」，分账记「支撑结论的原始材料」。
//
// 模型（辞玖 2026-09-11 定）：
//
//	总账（core 内，权威）      ← 只收状态行：action{id,name,target} + outcome + ref
//	分账（data/ledger/detail/<owner>/）← 每个 owner（插件/agent）自写详情，永久保留（暂不自动清理）
//	派生视图（agent 日志/控制台/统计） ← 从上面两者重建
//
// 铁律：**分账是"证据"，不是"权威"**。分账缺失不影响总账成立；
// 绝不为分账引入跨进程一致性（一旦要"送达确认/重传"，就退化成 P17 说的胶水层）。
package ledger

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// DetailRoot 分账根目录（相对 core 运行目录）
const DetailRoot = "detail"

// byRefTypes 走"分账"的事件类型（动作型：有原始材料可取）
var byRefTypes = map[string]bool{
	"platform.action.started": true,
	"platform.action.settled": true,
	"platform.action.timeout": true,
}

// DetailPolicy 事件类型的详情归属（供文档/校验用）：
//
//	inline = 状态行即全部；byref = 状态行 + ref 指向分账详情
func DetailPolicy(evType string) string {
	if byRefTypes[evType] {
		return "byref"
	}
	return "inline"
}

// DetailDir 某个 owner 的分账目录
func DetailDir(base, owner string) string {
	return filepath.Join(base, DetailRoot, safeOwner(owner))
}

// safeOwner 防路径穿越（owner id → 安全目录名）
func safeOwner(owner string) string {
	var b strings.Builder
	for _, r := range owner {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9',
			r == '.', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	if b.Len() == 0 {
		return "unknown"
	}
	return b.String()
}

// ListDetailOwners 列出所有有分账的 owner
func ListDetailOwners(base string) []string {
	entries, err := os.ReadDir(filepath.Join(base, DetailRoot))
	if err != nil {
		return nil
	}
	out := []string{}
	for _, e := range entries {
		if e.IsDir() {
			out = append(out, e.Name())
		}
	}
	sort.Strings(out)
	return out
}

// FindRef 两级查询第一步：在总账里按 action.id 找到状态行，取回它的分账引用
func FindRef(dir, actionID string) (ref *Ref, act *Action, out *Outcome) {
	files, err := ListLedgerFiles(dir)
	if err != nil {
		return nil, nil, nil
	}
	for i := len(files) - 1; i >= 0; i-- {
		lines, err := readLines(files[i])
		if err != nil {
			continue
		}
		for j := len(lines) - 1; j >= 0; j-- {
			var e Entry
			if err := json.Unmarshal(lines[j], &e); err != nil {
				continue
			}
			if e.Action != nil && e.Action.ID == actionID {
				return e.Ref, e.Action, e.Outcome
			}
		}
	}
	return nil, nil, nil
}

// ReadDetail 两级查询第二步：按 owner + action.id 读回分账详情（新→旧，截取 limit 条）。
// 分账不存在不是错误（铁律：证据尽力而为）。
func ReadDetail(base, owner, actionID string, limit int) ([]map[string]any, error) {
	if limit <= 0 {
		limit = 200
	}
	dir := DetailDir(base, owner)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return []map[string]any{}, nil
	}
	names := []string{}
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".jsonl") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	out := []map[string]any{}
	for i := len(names) - 1; i >= 0 && len(out) < limit; i-- {
		lines, err := readLines(filepath.Join(dir, names[i]))
		if err != nil {
			continue
		}
		for j := len(lines) - 1; j >= 0 && len(out) < limit; j-- {
			row := map[string]any{}
			if err := json.Unmarshal(lines[j], &row); err != nil {
				continue
			}
			if actionID != "" {
				if a, _ := row["action"].(string); a != actionID {
					continue
				}
			}
			row["_file"] = names[i]
			out = append(out, row)
		}
	}
	return out, nil
}

// DetailReport 单 owner 分账校验报告
type DetailReport struct {
	Owner    string     `json:"owner"`
	Files    []FileInfo `json:"files"`
	FirstSeq uint64     `json:"first_seq"`
	LastSeq  uint64     `json:"last_seq"`
	Lines    int        `json:"lines"`
	BadLines int        `json:"bad_lines"`
	Gaps     []Gap      `json:"gaps,omitempty"`
	OK       bool       `json:"ok"`
}

// VerifyDetail 校验全部分账：文件/seq 连续性/坏行/末尾半行（永久保留 → 不按时间清理）
func VerifyDetail(base string) ([]DetailReport, error) {
	reports := []DetailReport{}
	for _, owner := range ListDetailOwners(base) {
		r := DetailReport{Owner: owner, Files: []FileInfo{}}
		dir := filepath.Join(base, DetailRoot, owner)
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		names := []string{}
		for _, e := range entries {
			if !e.IsDir() && strings.HasSuffix(e.Name(), ".jsonl") {
				names = append(names, e.Name())
			}
		}
		sort.Strings(names)
		var prev uint64
		for _, n := range names {
			fi, err := scanDetailFile(filepath.Join(dir, n))
			if err != nil {
				continue
			}
			r.Files = append(r.Files, fi)
			r.Lines += fi.Lines
			r.BadLines += fi.BadLines
			if r.FirstSeq == 0 {
				r.FirstSeq = fi.FirstSeq
			}
			if fi.LastSeq > r.LastSeq {
				r.LastSeq = fi.LastSeq
			}
			if prev != 0 && fi.FirstSeq != 0 && fi.FirstSeq != prev+1 && fi.FirstSeq > prev+1 {
				r.Gaps = append(r.Gaps, Gap{AfterSeq: prev, BeforeSeq: fi.FirstSeq, Missing: fi.FirstSeq - prev - 1})
			}
			if fi.LastSeq != 0 {
				prev = fi.LastSeq
			}
		}
		r.OK = len(r.Gaps) == 0 && r.BadLines == 0
		reports = append(reports, r)
	}
	return reports, nil
}

// scanDetailFile 扫描分账文件（行结构：{seq, ts, owner, action, kind, payload}）
func scanDetailFile(path string) (FileInfo, error) {
	fi := FileInfo{Name: filepath.Base(path)}
	st, err := os.Stat(path)
	if err != nil {
		return fi, err
	}
	fi.Bytes = st.Size()
	f, err := os.Open(path)
	if err != nil {
		return fi, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 256*1024), 8*1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e struct {
			Seq uint64 `json:"seq"`
		}
		if err := json.Unmarshal(line, &e); err != nil {
			fi.BadLines++
			continue
		}
		fi.Lines++
		if fi.FirstSeq == 0 {
			fi.FirstSeq = e.Seq
		}
		fi.LastSeq = e.Seq
	}
	if err := sc.Err(); err != nil {
		return fi, err
	}
	if st.Size() > 0 {
		buf := make([]byte, 1)
		if _, err := f.ReadAt(buf, st.Size()-1); err == nil && buf[0] != '\n' {
			fi.TailHalf = true
		}
	}
	return fi, nil
}

// ---- 载荷 map → 结构体（action/ref 从载荷提成账本顶层字段）----

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func strOf(m map[string]any, k string) string {
	if m == nil {
		return ""
	}
	if s, ok := m[k].(string); ok {
		return s
	}
	return ""
}

func numOf(m map[string]any, k string) float64 {
	if m == nil {
		return 0
	}
	switch v := m[k].(type) {
	case float64:
		return v
	case int:
		return float64(v)
	case int64:
		return float64(v)
	case uint64:
		return float64(v)
	}
	return 0
}

func actionFromMap(m map[string]any) *Action {
	if m == nil {
		return nil
	}
	a := &Action{
		ID:             strOf(m, "id"),
		Name:           strOf(m, "name"),
		Target:         strOf(m, "target"),
		ArgsDigest:     strOf(m, "args_digest"),
		ExpectWithinMS: int64(numOf(m, "expect_within_ms")),
	}
	if a.ID == "" && a.Name == "" {
		return nil
	}
	return a
}

func refFromMap(m map[string]any) *Ref {
	if m == nil {
		return nil
	}
	r := &Ref{
		Owner:  strOf(m, "owner"),
		File:   strOf(m, "file"),
		Seq:    uint64(numOf(m, "seq")),
		Lines:  int(numOf(m, "lines")),
		Bytes:  int64(numOf(m, "bytes")),
		Digest: strOf(m, "digest"),
	}
	if r.Owner == "" && r.File == "" {
		return nil
	}
	return r
}
