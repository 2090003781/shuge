// 本文件：账本校验（verify）——"到底丢没丢"的唯一客观判据。
//
// 判据：seq 单调 +1 连续；坏行（解析失败）单独列出；轮转边界衔接。
// 用法：cmd/ledgercheck 打印报告；API `/api/ledger/verify` 同源返回。
package ledger

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// FileInfo 单个账本文件的信息
type FileInfo struct {
	Name     string `json:"name"`
	Bytes    int64  `json:"bytes"`
	Lines    int    `json:"lines"`
	BadLines int    `json:"bad_lines"`
	FirstSeq uint64 `json:"first_seq"`
	LastSeq  uint64 `json:"last_seq"`
	TailHalf bool   `json:"tail_half_line"` // 文件末尾是否为半行（未写完）
}

// Gap seq 缺口
type Gap struct {
	AfterSeq  uint64 `json:"after_seq"`
	BeforeSeq uint64 `json:"before_seq"`
	Missing   uint64 `json:"missing"`
}

// BadLine 坏行
type BadLine struct {
	File string `json:"file"`
	Line int    `json:"line"`
	Err  string `json:"err"`
}

// Report 校验报告
// Disorder seq 重复或回退。典型特征：两个 core 实例同时写同一账本（2026-09-12 实测事故：
// 控制台重启时旧实例还没退完，新实例就打开了账本，924/925 各写两次）。
type Disorder struct {
	File    string `json:"file"`
	Line    int    `json:"line"`
	Seq     uint64 `json:"seq"`
	PrevSeq uint64 `json:"prev_seq"`
}

type Report struct {
	Dir      string     `json:"dir"`
	Files    []FileInfo `json:"files"`
	FirstSeq uint64     `json:"first_seq"`
	LastSeq  uint64     `json:"last_seq"`
	Lines    int        `json:"lines"`
	BadLines []BadLine  `json:"bad_lines,omitempty"`
	Gaps     []Gap      `json:"gaps,omitempty"`
	Disorder []Disorder `json:"disorder,omitempty"`
	OK       bool       `json:"ok"`
	Summary  string     `json:"summary"`
}

// ListLedgerFiles 列出账本数据文件（按名字升序 = 轮转先后顺序）
func ListLedgerFiles(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	out := []string{}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		n := e.Name()
		if strings.HasPrefix(n, ledgerPrefix) && strings.HasSuffix(n, ".jsonl") {
			out = append(out, filepath.Join(dir, n))
		}
	}
	sort.Strings(out)
	return out, nil
}

// Verify 扫描目录下全部账本文件，返回校验报告。
func Verify(dir string) (*Report, error) {
	files, err := ListLedgerFiles(dir)
	if err != nil {
		return nil, err
	}
	rep := &Report{Dir: dir, Files: []FileInfo{}}
	var prevSeq uint64
	for _, path := range files {
		fi, err := scanFile(path)
		if err != nil {
			return nil, err
		}
		rep.Files = append(rep.Files, fi)
		rep.Lines += fi.Lines
		if rep.FirstSeq == 0 && fi.FirstSeq != 0 {
			rep.FirstSeq = fi.FirstSeq
		}
		if fi.LastSeq > rep.LastSeq {
			rep.LastSeq = fi.LastSeq
		}
		// 轮转边界：上一个文件的末 seq 与下一个文件的首 seq 必须衔接
		if prevSeq != 0 && fi.FirstSeq != 0 && fi.FirstSeq != prevSeq+1 {
			if fi.FirstSeq > prevSeq+1 {
				rep.Gaps = append(rep.Gaps, Gap{AfterSeq: prevSeq, BeforeSeq: fi.FirstSeq, Missing: fi.FirstSeq - prevSeq - 1})
			}
		}
		if fi.LastSeq != 0 {
			prevSeq = fi.LastSeq
		}
	}
	// 文件内缺口
	for _, fi := range rep.Files {
		path := filepath.Join(dir, fi.Name)
		gaps, bads, dis, err := scanGaps(path)
		if err != nil {
			return nil, err
		}
		rep.Gaps = append(rep.Gaps, gaps...)
		rep.BadLines = append(rep.BadLines, bads...)
		rep.Disorder = append(rep.Disorder, dis...)
	}
	rep.OK = len(rep.Gaps) == 0 && len(rep.BadLines) == 0 && len(rep.Disorder) == 0
	if rep.OK {
		rep.Summary = fmt.Sprintf("无缺口：共 %d 条记录，seq %d→%d，文件 %d 个",
			rep.Lines, rep.FirstSeq, rep.LastSeq, len(rep.Files))
	} else {
		rep.Summary = fmt.Sprintf("异常：缺口 %d 处、坏行 %d 处（共 %d 条记录，seq %d→%d）",
			len(rep.Gaps), len(rep.BadLines), rep.Lines, rep.FirstSeq, rep.LastSeq)
	}
	if len(rep.Disorder) > 0 {
		rep.Summary += fmt.Sprintf("；另发现 seq 重复/回退 %d 处（两个写者同时写账本？）", len(rep.Disorder))
	}
	return rep, nil
}

// scanFile 统计单文件行数与首末 seq（顺带标记末尾半行）
func scanFile(path string) (FileInfo, error) {
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
		var e Entry
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
	// 末尾半行检测：最后一个字节不是换行
	if st.Size() > 0 {
		buf := make([]byte, 1)
		if _, err := f.ReadAt(buf, st.Size()-1); err == nil && buf[0] != '\n' {
			fi.TailHalf = true
		}
	}
	return fi, nil
}

// scanGaps 单文件内 seq 连续性 + 坏行明细
func scanGaps(path string) ([]Gap, []BadLine, []Disorder, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, nil, nil, err
	}
	defer f.Close()
	gaps := []Gap{}
	bads := []BadLine{}
	dis := []Disorder{}
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 256*1024), 8*1024*1024)
	n := 0
	var prev uint64
	for sc.Scan() {
		n++
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var e Entry
		if err := json.Unmarshal(line, &e); err != nil {
			bads = append(bads, BadLine{File: filepath.Base(path), Line: n, Err: err.Error()})
			continue
		}
		if prev != 0 && e.Seq > prev+1 {
			gaps = append(gaps, Gap{AfterSeq: prev, BeforeSeq: e.Seq, Missing: e.Seq - prev - 1})
		}
		// 重复/回退单独记：这不是"丢记录"，而是"两个写者"——比缺口更严重（静默损坏）
		if prev != 0 && e.Seq <= prev {
			dis = append(dis, Disorder{File: filepath.Base(path), Line: n, Seq: e.Seq, PrevSeq: prev})
		}
		prev = e.Seq
	}
	if err := sc.Err(); err != nil {
		return gaps, bads, dis, err
	}
	return gaps, bads, dis, nil
}
