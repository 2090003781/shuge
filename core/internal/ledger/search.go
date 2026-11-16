// 本文件：账本检索（把"查账本"从旧的日志模块搬到账本包）。
//
// 用途：控制台账本页 / `GET /api/events/search`。
// 语义变化（2026-09-11 改造）：账本本期只收 `platform` 域，
// 因此 level=agent 会返回空——agent 日志将来由 external 插件从账本派生。
package ledger

import (
	"bufio"
	"encoding/json"
	"os"
	"sort"
	"strings"
	"time"
)

// SearchParams 查询参数（与旧接口字段保持兼容）
type SearchParams struct {
	Level   string // all | platform | agent | plugin | vault | system(旧别名→platform)
	Agent   string // 兼容旧参数：匹配 payload.agent 或 source
	Type    string // 事件类型前缀
	Source  string // 来源精确匹配
	Keyword string // 大小写不敏感全文（type/source/payload）
	From    string // RFC3339 起始
	To      string // RFC3339 截止
	Limit   int    // 最大条数（默认 50，取最新）
}

// Searcher 账本查询器（实现 api.EventLogSearcher）
type Searcher struct {
	Dir string
}

// Search 满足 api 的查询接口
func (s Searcher) Search(p SearchParams) ([]map[string]any, error) {
	return Search(s.Dir, p)
}

// Search 扫描账本文件，返回匹配记录（新→旧，截取 limit 条）
func Search(dir string, p SearchParams) ([]map[string]any, error) {
	if p.Limit <= 0 {
		p.Limit = 50
	}
	files, err := ListLedgerFiles(dir)
	if err != nil {
		return nil, err
	}
	from, _ := time.Parse(time.RFC3339, p.From)
	to, _ := time.Parse(time.RFC3339, p.To)
	kw := strings.ToLower(p.Keyword)

	out := []map[string]any{}
	// 从最新文件往前扫，凑够 limit 就停
	for i := len(files) - 1; i >= 0 && len(out) < p.Limit; i-- {
		lines, err := readLines(files[i])
		if err != nil {
			continue
		}
		for j := len(lines) - 1; j >= 0 && len(out) < p.Limit; j-- {
			var e Entry
			if err := json.Unmarshal(lines[j], &e); err != nil {
				continue
			}
			if !matchEntry(e, p, kw, from, to) {
				continue
			}
			out = append(out, toRow(e))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return asString(out[i]["ts"]) > asString(out[j]["ts"])
	})
	if len(out) > p.Limit {
		out = out[:p.Limit]
	}
	return out, nil
}

// ReadAfter 位点读取：返回 seq > after 的记录（供未来的 agent 日志插件追赶消费）
func ReadAfter(dir string, after uint64, limit int) ([]Entry, error) {
	if limit <= 0 {
		limit = 200
	}
	files, err := ListLedgerFiles(dir)
	if err != nil {
		return nil, err
	}
	out := []Entry{}
	for _, f := range files {
		lines, err := readLines(f)
		if err != nil {
			continue
		}
		for _, ln := range lines {
			var e Entry
			if err := json.Unmarshal(ln, &e); err != nil {
				continue
			}
			if e.Seq <= after {
				continue
			}
			out = append(out, e)
			if len(out) >= limit {
				return out, nil
			}
		}
	}
	return out, nil
}

func matchEntry(e Entry, p SearchParams, kw string, from, to time.Time) bool {
	// 层级 → 域
	switch p.Level {
	case "", "all":
		// 不过滤
	case "system":
		if e.Realm != RealmPlatform { // 旧别名
			return false
		}
	default:
		if e.Realm != p.Level {
			return false
		}
	}
	if p.Agent != "" {
		a, _ := e.Payload["agent"].(string)
		if a != p.Agent && e.Source != p.Agent {
			return false
		}
	}
	if p.Type != "" && !strings.HasPrefix(e.Type, p.Type) {
		return false
	}
	if p.Source != "" && e.Source != p.Source {
		return false
	}
	if !from.IsZero() || !to.IsZero() {
		if t, err := time.Parse(time.RFC3339, e.TS); err == nil {
			if !from.IsZero() && t.Before(from) {
				return false
			}
			if !to.IsZero() && t.After(to) {
				return false
			}
		}
	}
	if kw != "" {
		hay := strings.ToLower(e.Type + " " + e.Source + " " + jsonSprint(e.Payload))
		if !strings.Contains(hay, kw) {
			return false
		}
	}
	return true
}

func toRow(e Entry) map[string]any {
	row := map[string]any{
		"seq":    e.Seq,
		"ts":     e.TS,
		"type":   e.Type,
		"source": e.Source,
		"target": e.Target,
		"realm":  e.Realm,
		"class":  e.Class,
	}
	if len(e.Scope) > 0 {
		row["scope"] = e.Scope
	}
	if e.Outcome != nil {
		row["outcome"] = e.Outcome
	}
	if e.Payload != nil {
		row["payload"] = e.Payload
	}
	return row
}

func readLines(path string) ([][]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 256*1024), 8*1024*1024)
	out := [][]byte{}
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		cp := make([]byte, len(line))
		copy(cp, line)
		out = append(out, cp)
	}
	return out, sc.Err()
}

func jsonSprint(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	return string(b)
}

func asString(v any) string {
	s, _ := v.(string)
	return s
}
