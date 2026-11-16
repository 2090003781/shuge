// 本文件：平台账本写入核心（唯一真相源）。
//
// 定位（2026-09-11 辞玖拍板）：
//
//	· 账本 = **平台自身**的一切记录（core / 进程监督 / 总线 / 组件 / 配置 / 账本自身）
//	· 日志 = agent 的活动记录 → 派生视图，将来由 external 插件从账本消费生成
//	· 只有本包写账本文件（单写者）；插件与 agent 一律经平台上报
//
// 六条硬要求（对应辞玖的"全记录、不丢、不卡、不崩溃、尽量快、不被堵塞"）：
//  1. 先写后播：Publish 时先落账本，再扇出总线（账本不可丢，扇出 best-effort）
//  2. 组提交：攒 CommitEvery(默认 10ms) 或 CommitMax(默认 500) 条 → 一次 write + 一次 fsync
//     （Publish 不再逐个等盘，吞吐因此不被 fsync 钉死）
//  3. 分级：critical 同步档（等本批落盘才返回，绝不丢）／business 批量档／telemetry 可丢
//  4. 可恢复：启动扫最新文件末尾，半行截断（只截尾，不废整文件）；seq 从末行延续
//  5. 写失败留痕：应急文件 + 计数 + degraded 状态；critical 进积压（有上限）
//  6. 可校验：seq 单调连续，缺口由 verify 报出（"丢没丢"的唯一客观判据）
package ledger

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"core/internal/plugin"
)

const (
	defaultMaxFileBytes = 64 << 20 // 单文件轮转阈值
	defaultKeepDays     = 0        // 历史账本保留天数（0 = 永久保留；2026-09-11 辞玖定：先永久，观察后再调）
	defaultCommitEvery  = 10 * time.Millisecond
	defaultCommitMax    = 500
	defaultPendingMax   = 20000
	defaultCriticalCap  = 10000
	criticalWaitTimeout = 2 * time.Second
	errorNoticeEvery    = 30 * time.Second
	ledgerPrefix        = "ledger-"
	emergencyPrefix     = "emergency-"
	SchemaVersion       = 1
)

// Entry 账本一行（信封 + 载荷）。字段一旦发布不得改名（历史账本要能对得上）。
type Entry struct {
	Seq     uint64         `json:"seq"`    // 账本内单调递增；缺口即异常
	ID      string         `json:"id"`     // 全局唯一（幂等键）
	TS      string         `json:"ts"`     // 挂钟时间（本地 RFC3339）
	Mono    int64          `json:"mono"`   // 单调时钟（UnixNano，防时钟跳变）
	Schema  int            `json:"schema"` // 结构版本
	Realm   string         `json:"realm"`  // 对象域（platform/agent/plugin/vault）
	Class   string         `json:"class"`  // 落盘档位（critical/business/telemetry）
	Type    string         `json:"type"`   // 事件类型（ASCII 点分，稳定）
	Source  string         `json:"source"` // 谁报的
	Scope   []string       `json:"scope,omitempty"`
	Target  string         `json:"target,omitempty"`
	Outcome *Outcome       `json:"outcome,omitempty"` // 成功/失败（动作类事件）
	Action  *Action        `json:"action,omitempty"`  // 动作描述符（总账只记"干了什么"）
	Ref     *Ref           `json:"ref,omitempty"`     // 分账引用（详情在哪，见 detail.go）
	Payload map[string]any `json:"payload,omitempty"` // 状态行的补充字段（详情不进这里）
	Trace   string         `json:"trace,omitempty"`
	Parent  string         `json:"parent,omitempty"`
}

// Outcome 动作结果（"成功了还是失败了"就记在这里）
type Outcome struct {
	Status     string `json:"status"` // ok|error|timeout|denied|skipped
	Code       string `json:"code,omitempty"`
	Message    string `json:"message,omitempty"`
	DurationMS int64  `json:"duration_ms,omitempty"`
}

// Action 动作描述符：总账记"谁、对什么、干了什么"；参数只留摘要（隐私 + 体积）
type Action struct {
	ID             string `json:"id"`                         // 动作唯一 id（两段式共用；幂等键）
	Name           string `json:"name"`                       // 动作名，如 context.pack.write
	Target         string `json:"target,omitempty"`           // 对象（文件/agent/服务）
	ArgsDigest     string `json:"args_digest,omitempty"`      // 参数摘要（原文不进账本）
	ExpectWithinMS int64  `json:"expect_within_ms,omitempty"` // 发起时声明：多久没结束算超时
}

// Ref 分账引用：详情在哪（总账 = 结论 + 指针；分账 = 证据）
type Ref struct {
	Owner  string `json:"owner"`            // 分账归属（插件/agent id）
	File   string `json:"file"`             // 分账文件名
	Seq    uint64 `json:"seq,omitempty"`    // 该动作在分账中的起始 seq
	Lines  int    `json:"lines,omitempty"`  // 占用行数
	Bytes  int64  `json:"bytes,omitempty"`  // 占用字节
	Digest string `json:"digest,omitempty"` // 可选摘要（举证时再算，默认不记）
}

// Result Append 的结果
type Result string

const (
	ResultWritten Result = "written" // 已落盘（critical 同步档）
	ResultQueued  Result = "queued"  // 已入批（等待组提交）
	ResultSkipped Result = "skipped" // 域不收录（本期只收 platform）
	ResultDropped Result = "dropped" // 按降级阶梯丢弃（已计数留痕）
)

// Options 账本选项（零值即默认）
type Options struct {
	Dir          string
	MaxFileBytes int64
	KeepDays     int
	CommitEvery  time.Duration
	CommitMax    int
	PendingMax   int
	Realms       []string // 收录的对象域（默认 ["platform"]）
	Logger       *slog.Logger
}

// Stats 账本运行状态（供 /api/status 展示）
type Stats struct {
	File             string            `json:"file"`
	LastSeq          uint64            `json:"last_seq"`
	LastWriteAt      string            `json:"last_write_at"`
	Pending          int               `json:"pending"`
	Written          uint64            `json:"written"`
	WriteErrors      uint64            `json:"write_errors"`
	DroppedBusiness  uint64            `json:"dropped_business"`
	DroppedTelemetry uint64            `json:"dropped_telemetry"`
	Degraded         bool              `json:"degraded"`
	CriticalBacklog  int               `json:"critical_backlog"`
	Rotations        uint64            `json:"rotations"`
	TruncatedBytes   int64             `json:"truncated_bytes"`
	UnknownTypes     map[string]uint64 `json:"unknown_types,omitempty"`
}

type rec struct {
	entry Entry
	done  chan Result // 仅 critical 使用（等本批落盘）
}

// Ledger 平台账本
type Ledger struct {
	opts   Options
	log    *slog.Logger
	realms map[string]bool

	mu        sync.Mutex
	seq       uint64
	pending   []*rec
	critBack  []*rec
	file      *os.File
	filePath  string
	fileBytes int64
	closed    bool
	stats     Stats

	lastErrorNotice time.Time
	resumePath      string // 启动时续写的目标文件（避免每次重启都开新文件）

	wake     chan struct{}
	stop     chan struct{}
	done     chan struct{}
	flushReq chan chan struct{}

	// 由 main 注入：轮转/写错时的事件留痕（避免本包反向依赖总线）
	onRotate   func(oldFile, newFile string, bytes int64)
	openRotate *rotateInfo // 开局即轮转的信息（钩子还没装上，由 main 取了补发事件）
	onError    func(err error, emergency string, backlog int)

	// faultWrite 故障注入缝（测试用）：非 nil 时 writeAndSync 直接返回其错误，
	// 用于回归"写盘失败 → 应急文件 + 计数 + degraded + critical 进积压"这条路径。
	faultWrite func(data []byte) error
}

// Open 打开（或创建）账本：恢复 seq、截断半行、启动组提交循环。
func Open(opts Options) (*Ledger, error) {
	if opts.Dir == "" {
		opts.Dir = filepath.Join("data", "ledger")
	}
	if opts.MaxFileBytes <= 0 {
		opts.MaxFileBytes = defaultMaxFileBytes
	}
	if opts.KeepDays <= 0 {
		opts.KeepDays = defaultKeepDays
	}
	if opts.CommitEvery <= 0 {
		opts.CommitEvery = defaultCommitEvery
	}
	if opts.CommitMax <= 0 {
		opts.CommitMax = defaultCommitMax
	}
	if opts.PendingMax <= 0 {
		opts.PendingMax = defaultPendingMax
	}
	if len(opts.Realms) == 0 {
		opts.Realms = []string{RealmPlatform}
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if err := os.MkdirAll(opts.Dir, 0o755); err != nil {
		return nil, fmt.Errorf("ledger mkdir: %w", err)
	}
	l := &Ledger{
		opts:     opts,
		log:      opts.Logger,
		realms:   map[string]bool{},
		wake:     make(chan struct{}, 1),
		stop:     make(chan struct{}),
		done:     make(chan struct{}),
		flushReq: make(chan chan struct{}),
	}
	for _, r := range opts.Realms {
		l.realms[r] = true
	}
	if err := l.recover(); err != nil {
		return nil, err
	}
	if err := l.openFile(); err != nil {
		return nil, err
	}
	go l.loop()
	go l.cleanupOldFiles()
	l.log.Info("账本已打开", "dir", opts.Dir, "resume_seq", l.seq,
		"truncated_bytes", l.stats.TruncatedBytes, "realms", opts.Realms)
	return l, nil
}

// Dir 账本目录
func (l *Ledger) Dir() string { return l.opts.Dir }

// SetHooks 注入轮转/写错的事件留痕回调（main 装配时调用）
func (l *Ledger) SetHooks(onRotate func(oldFile, newFile string, bytes int64), onError func(err error, emergency string, backlog int)) {
	l.onRotate = onRotate
	l.onError = onError
}

// Append 记账（满足 eventbus.Appender —— 总线在扇出之前调用）
func (l *Ledger) Append(ev plugin.Event) { _ = l.AppendResult(ev) }

// AppendResult 记账并返回结果：critical 同步档等本批 fsync 落盘；其余档只入批
func (l *Ledger) AppendResult(ev plugin.Event) Result {
	spec, known := Classify(ev.Type)
	// 漏登记的平台事件类型要计数（catalog 缺口的可见化）：照写不误，但必须被看见
	if !known && spec.Realm == RealmPlatform {
		l.countUnknown(ev.Type)
	}
	if !l.realms[spec.Realm] {
		return ResultSkipped
	}

	r := &rec{}
	l.mu.Lock()
	if l.closed {
		l.mu.Unlock()
		return ResultSkipped
	}
	l.seq++
	r.entry = buildEntry(ev, spec, l.seq)
	l.stats.LastSeq = l.seq
	if spec.Class == ClassCritical {
		r.done = make(chan Result, 1)
	}
	// 背压阶梯：待写超限时非关键档丢弃（留痕计数），关键档继续排队
	if len(l.pending) >= l.opts.PendingMax && spec.Class != ClassCritical {
		if spec.Class == ClassTelemetry {
			l.stats.DroppedTelemetry++
		} else {
			l.stats.DroppedBusiness++
		}
		l.mu.Unlock()
		return ResultDropped
	}
	l.pending = append(l.pending, r)
	needKick := len(l.pending) >= l.opts.CommitMax
	l.mu.Unlock()
	if needKick {
		l.kick()
	}
	if r.done != nil {
		select {
		case res := <-r.done:
			return res
		case <-time.After(criticalWaitTimeout):
			return ResultQueued // 落盘过慢时不拖住调用方，由积压机制兜底
		}
	}
	return ResultQueued
}

// Flush 立即提交当前批次（优雅停机用）
func (l *Ledger) Flush() {
	ch := make(chan struct{})
	select {
	case l.flushReq <- ch:
		select {
		case <-ch:
		case <-l.done:
		}
	case <-l.done:
	}
}

// Stats 当前状态快照
func (l *Ledger) Stats() Stats {
	l.mu.Lock()
	defer l.mu.Unlock()
	s := l.stats
	s.Pending = len(l.pending)
	s.CriticalBacklog = len(l.critBack)
	s.File = filepath.Base(l.filePath)
	if s.UnknownTypes != nil {
		cp := make(map[string]uint64, len(s.UnknownTypes))
		for k, v := range s.UnknownTypes {
			cp[k] = v
		}
		s.UnknownTypes = cp
	}
	return s
}

// Close 提交剩余批次并关闭文件
func (l *Ledger) Close() error {
	l.mu.Lock()
	if l.closed {
		l.mu.Unlock()
		return nil
	}
	l.closed = true
	l.mu.Unlock()
	close(l.stop)
	<-l.done
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file != nil {
		_ = l.file.Sync()
		_ = l.file.Close()
		l.file = nil
	}
	return nil
}

// ---- 内部：写入循环 ----

func (l *Ledger) loop() {
	t := time.NewTicker(l.opts.CommitEvery)
	defer t.Stop()
	defer close(l.done)
	for {
		select {
		case <-l.stop:
			l.flush()
			return
		case <-l.wake:
			l.flush()
		case req := <-l.flushReq:
			l.flush()
			close(req)
		case <-t.C:
			l.flush()
		}
	}
}

func (l *Ledger) kick() {
	select {
	case l.wake <- struct{}{}:
	default:
	}
}

// flush 组提交：一次 write + 一次 fsync 写完整批
func (l *Ledger) flush() {
	l.mu.Lock()
	if len(l.pending) == 0 && len(l.critBack) == 0 {
		l.mu.Unlock()
		return
	}
	batch := l.pending
	l.pending = nil
	nBacklog := len(l.critBack)
	if nBacklog > 0 {
		// 关键积压优先补写；**写成功之后才出队**——写失败时原样保留，
		// 避免"取走→失败→放回"这段空窗被读到（也会让重复入账的风险归零）
		batch = append(append([]*rec{}, l.critBack...), batch...)
	}
	l.mu.Unlock()

	var buf bytes.Buffer
	for _, r := range batch {
		b, err := json.Marshal(r.entry)
		if err != nil {
			l.log.Error("ledger: 编码失败", "type", r.entry.Type, "err", err)
			continue
		}
		buf.Write(b)
		buf.WriteByte('\n')
	}
	data := buf.Bytes()
	if len(data) == 0 {
		return
	}

	if l.fileBytes+int64(len(data)) > l.opts.MaxFileBytes {
		// 注意：不加 l.fileBytes>0 保护 —— 首个批次本身就可能超阈值（测试网实测 200 条一次入批），
		// 加了保护会导致"永不轮转"。代价是可能留下一个 0 字节碎片文件（无害，且排序已修）。
		l.rotate()
	}
	if err := l.writeAndSync(data); err != nil {
		l.onWriteError(batch, data, err, nBacklog)
		return
	}
	for _, r := range batch {
		if r.done != nil {
			select {
			case r.done <- ResultWritten:
			default:
			}
		}
	}
	l.mu.Lock()
	if nBacklog > 0 && len(l.critBack) >= nBacklog {
		l.critBack = l.critBack[nBacklog:] // 已落盘的那部分积压出队
	}
	l.stats.Written += uint64(len(batch))
	l.stats.LastWriteAt = time.Now().Format(time.RFC3339)
	l.stats.Degraded = false
	l.mu.Unlock()
}

func (l *Ledger) writeAndSync(data []byte) error {
	if l.faultWrite != nil { // 测试注入：模拟磁盘写失败
		return l.faultWrite(data)
	}
	l.mu.Lock()
	f := l.file
	l.mu.Unlock()
	if f == nil {
		return fmt.Errorf("ledger: 文件未打开")
	}
	if _, err := f.Write(data); err != nil {
		return err
	}
	if err := f.Sync(); err != nil { // 一次 fsync 覆盖整批 = 组提交的核心
		return err
	}
	l.mu.Lock()
	l.fileBytes += int64(len(data))
	l.mu.Unlock()
	return nil
}

// onWriteError 写盘失败：应急落盘 + 计数 + 关键事件进积压（留痕，绝不静默）
func (l *Ledger) onWriteError(batch []*rec, data []byte, err error, nBacklog int) {
	name := emergencyPrefix + time.Now().Format("20060102-150405.000") + ".jsonl"
	path := filepath.Join(l.opts.Dir, name)
	if werr := os.WriteFile(path, data, 0o644); werr != nil {
		name = "(应急文件也写入失败: " + werr.Error() + ")"
	}

	l.mu.Lock()
	l.stats.WriteErrors++
	l.stats.Degraded = true
	criticalInBatch := 0
	for i, r := range batch {
		switch Class(r.entry.Class) {
		case ClassCritical:
			criticalInBatch++
			// batch 前 nBacklog 条本来就出自 critBack（写成功才出队，不重复入队）；
			// 之后的是本批新进来的关键事件 → 必须进积压，绝不丢
			if i >= nBacklog && len(l.critBack) < defaultCriticalCap {
				l.critBack = append(l.critBack, r)
			}
		case ClassTelemetry:
			l.stats.DroppedTelemetry++
		default:
			l.stats.DroppedBusiness++
		}
	}
	backlog := len(l.critBack)
	notify := time.Since(l.lastErrorNotice) >= errorNoticeEvery
	if notify {
		l.lastErrorNotice = time.Now()
	}
	l.mu.Unlock()

	l.log.Error("ledger: 写盘失败", "err", err, "emergency", name, "critical_backlog", backlog)
	if notify && l.onError != nil {
		go l.onError(err, name, backlog) // 异步：避免与总线互相触发
	}
	for _, r := range batch {
		if r.done != nil {
			select {
			case r.done <- ResultQueued:
			default:
			}
		}
	}
}

// rotate 轮转：新文件先开成功再关旧文件（开不了就继续用旧文件，不影响写入）
func (l *Ledger) rotate() {
	l.mu.Lock()
	old := l.filePath
	oldBytes := l.fileBytes
	oldFile := l.file
	l.mu.Unlock()

	// 文件名只到秒 → 同一秒内多次轮转会**撞名**（新旧文件同名 = 轮转到自己，
	// 文件继续变大且计数虚增）。测试网第一次跑就抓到这里（阈值 2KB 时一秒轮转多次）。
	// 撞名就加序号：ledger-20260912-211327_2.jsonl —— 分隔符必须用 `_`（0x5F）而不是 `-`（0x2D）：\n	// 文件名比较时 `-` 比 `.` 小，会让带序号的更早文件被当成"更新"，而"最新文件"是靠名字排序找的（测试网抓到）。
	base := ledgerPrefix + time.Now().Format("20060102-150405")
	p := filepath.Join(l.opts.Dir, base+".jsonl")
	for i := 2; p == old || fileExists(p); i++ {
		p = filepath.Join(l.opts.Dir, fmt.Sprintf("%s_%d.jsonl", base, i))
	}
	nf, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		l.log.Error("ledger: 轮转失败（继续写原文件）", "err", err)
		return
	}
	l.mu.Lock()
	l.file = nf
	l.filePath = p
	l.fileBytes = 0
	l.stats.Rotations++
	l.mu.Unlock()
	if oldFile != nil {
		_ = oldFile.Sync()
		_ = oldFile.Close()
	}
	l.log.Info("ledger: 轮转", "old", filepath.Base(old), "new", filepath.Base(p), "old_bytes", oldBytes)
	if l.onRotate != nil {
		l.onRotate(filepath.Base(old), filepath.Base(p), oldBytes)
	}
}

// cleanupOldFiles 清理超过 KeepDays 的历史账本（保留最新一个）
func (l *Ledger) cleanupOldFiles() {
	if l.opts.KeepDays <= 0 { // 0 = 永久保留
		return
	}
	files, err := ListLedgerFiles(l.opts.Dir)
	if err != nil || len(files) <= 1 {
		return
	}
	cut := time.Now().AddDate(0, 0, -l.opts.KeepDays)
	for _, f := range files[:len(files)-1] { // 永远保留最新文件
		st, err := os.Stat(f)
		if err != nil || st.ModTime().After(cut) {
			continue
		}
		if err := os.Remove(f); err == nil {
			l.log.Info("ledger: 清理历史账本", "file", filepath.Base(f))
		}
	}
}

// ---- 内部：恢复与文件 ----

func (l *Ledger) recover() error {
	files, err := ListLedgerFiles(l.opts.Dir)
	if err != nil {
		return err
	}
	if len(files) == 0 {
		return nil
	}
	last := files[len(files)-1]
	seq, truncated, err := truncateAndReadTail(last)
	if err != nil {
		return fmt.Errorf("ledger recover: %w", err)
	}
	l.seq = seq
	l.stats.LastSeq = seq
	l.stats.TruncatedBytes = truncated
	l.resumePath = last
	if truncated > 0 {
		l.log.Warn("ledger: 末尾半行已截断（只截尾，不废整文件）",
			"file", filepath.Base(last), "bytes", truncated, "resume_seq", seq)
	}
	return nil
}

// truncateAndReadTail 截断末尾半行并取回最后一条完整记录的 seq
func truncateAndReadTail(path string) (uint64, int64, error) {
	f, err := os.OpenFile(path, os.O_RDWR, 0o644)
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return 0, 0, err
	}
	size := st.Size()
	if size == 0 {
		return 0, 0, nil
	}
	const tailN = 256 << 10
	start := int64(0)
	if size > tailN {
		start = size - tailN
	}
	buf := make([]byte, size-start)
	if _, err := f.ReadAt(buf, start); err != nil {
		return 0, 0, err
	}
	var truncated int64
	if len(buf) > 0 && buf[len(buf)-1] != '\n' {
		idx := bytes.LastIndexByte(buf, '\n')
		var newSize int64
		if idx < 0 {
			newSize = start
		} else {
			newSize = start + int64(idx) + 1
		}
		if err := f.Truncate(newSize); err != nil {
			return 0, 0, err
		}
		truncated = size - newSize
		if idx < 0 {
			buf = nil
		} else {
			buf = buf[:idx+1]
		}
	}
	lines := bytes.Split(bytes.TrimRight(buf, "\n"), []byte{'\n'})
	for i := len(lines) - 1; i >= 0 && i >= len(lines)-200; i-- {
		if len(bytes.TrimSpace(lines[i])) == 0 {
			continue
		}
		var e Entry
		if err := json.Unmarshal(lines[i], &e); err == nil {
			return e.Seq, truncated, nil
		}
	}
	return 0, truncated, nil
}

func (l *Ledger) openFile() error {
	base := ledgerPrefix + time.Now().Format("20060102-150405")
	p := filepath.Join(l.opts.Dir, base+".jsonl")
	for i := 2; fileExists(p); i++ { // 同秒同名文件已存在 → 加序号（否则会续写到别人的文件里）
		p = filepath.Join(l.opts.Dir, fmt.Sprintf("%s_%d.jsonl", base, i))
	}
	// 有可续写的最新文件且未达轮转阈值 → 续写它（重启不产生碎片文件）
	if l.resumePath != "" {
		if st, err := os.Stat(l.resumePath); err == nil {
			if st.Size() < l.opts.MaxFileBytes {
				p = l.resumePath
			} else {
				// 开局即轮转：上一个文件已超阈值，这次开新文件。
				// 以前这里**默默新建**、既不计轮转数也不触发钩子（演练 C 挖出的监控盲区）——
				// 结果"轮转这件事发生过"在状态与账本里都看不到。
				l.openRotate = &rotateInfo{Old: filepath.Base(l.resumePath), New: filepath.Base(p), Bytes: st.Size()}
				l.stats.Rotations++
			}
		}
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("ledger open: %w", err)
	}
	l.file = f
	l.filePath = p
	if st, err := f.Stat(); err == nil {
		l.fileBytes = st.Size()
	}
	return nil
}

func (l *Ledger) countUnknown(typ string) {
	l.mu.Lock()
	if l.stats.UnknownTypes == nil {
		l.stats.UnknownTypes = map[string]uint64{}
	}
	l.stats.UnknownTypes[typ]++
	l.mu.Unlock()
}

// ---- 内部：信封构造 ----

func buildEntry(ev plugin.Event, spec Spec, seq uint64) Entry {
	ts := ev.Time
	if ts.IsZero() {
		ts = time.Now()
	}
	// action/ref 从载荷提成账本顶层字段（先复制再删，避免改动原 map——同一事件还要扇出给订阅者）
	payload := ev.Payload
	var act *Action
	var ref *Ref
	if payload != nil {
		_, hasAct := payload["action"]
		_, hasRef := payload["ref"]
		_, hasOut := payload["outcome"]
		if hasAct || hasRef || hasOut {
			cp := make(map[string]any, len(payload))
			for k, v := range payload {
				cp[k] = v
			}
			if hasAct {
				act = actionFromMap(asMap(cp["action"]))
				delete(cp, "action")
			}
			if hasRef {
				ref = refFromMap(asMap(cp["ref"]))
				delete(cp, "ref")
			}
			if hasOut {
				delete(cp, "outcome") // 已在顶层 outcome 字段，载荷里不再重复
			}
			payload = cp
		}
	}
	return Entry{
		Seq:     seq,
		ID:      entryID(ev, seq),
		TS:      ts.Format(time.RFC3339),
		Mono:    time.Now().UnixNano(),
		Schema:  SchemaVersion,
		Realm:   spec.Realm,
		Class:   string(spec.Class),
		Type:    ev.Type,
		Source:  ev.Source,
		Scope:   ev.Scope,
		Target:  ev.Target,
		Outcome: outcomeOf(ev.Payload),
		Action:  act,
		Ref:     ref,
		Payload: payload,
	}
}

func entryID(ev plugin.Event, seq uint64) string {
	if ev.ID != "" {
		return ev.ID
	}
	return fmt.Sprintf("%d-%d-%s", seq, os.Getpid(), time.Now().Format("150405.000000"))
}

// outcomeOf 从载荷里提取"成功/失败"（有则记，没有不编）
func outcomeOf(p map[string]any) *Outcome {
	if p == nil {
		return nil
	}
	if raw, ok := p["outcome"].(map[string]any); ok {
		o := &Outcome{}
		if v, ok := raw["status"].(string); ok {
			o.Status = v
		}
		if v, ok := raw["code"].(string); ok {
			o.Code = v
		}
		if v, ok := raw["message"].(string); ok {
			o.Message = truncate(v, 500)
		}
		if o.Status == "" {
			o.Status = "ok"
		}
		return o
	}
	if v, ok := p["ok"].(bool); ok {
		o := &Outcome{Status: "ok"}
		if !v {
			o.Status = "error"
		}
		if msg, ok := p["error"].(string); ok && msg != "" {
			o.Message = truncate(msg, 500)
		}
		return o
	}
	for _, k := range []string{"error", "err"} {
		if msg, ok := p[k].(string); ok && msg != "" {
			return &Outcome{Status: "error", Message: truncate(msg, 500)}
		}
	}
	return nil
}

// truncate 截断显示用字符串：**按 rune 切**，否则中文会被截成半个字
// （2026-09-20 审计发现：这里原来用 s[:n] 按字节切，而 cmd/core-host/check.go 里那份是按 rune 的 —— 两份同名不同义）。
func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

// rotateInfo 一次轮转的前后信息
type rotateInfo struct {
	Old   string `json:"old"`
	New   string `json:"new"`
	Bytes int64  `json:"bytes"`
}

// TakeOpenRotation 取走"开局即轮转"的信息（取一次就清空；main 在装好钩子后补发事件）
func (l *Ledger) TakeOpenRotation() (rotateInfo, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.openRotate == nil {
		return rotateInfo{}, false
	}
	info := *l.openRotate
	l.openRotate = nil
	return info, true
}

// fileExists 文件是否存在（轮转/开文件时防撞名用）
func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}
