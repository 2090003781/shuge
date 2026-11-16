// Package vault Vault 文件资源：完整文件树 + 通用文件读取（只读）。
package vault

import (
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// 排除的顶层目录/路径（Vault 系统目录、私密、临时）
var ExcludePaths = []string{
	".obsidian", ".trash", ".git",
	"个人/私密",
	"日志/reasonix-raw",
	"tmp",
}

// Vault 只读文件资源
type Vault struct {
	root string
}

// New 创建 Vault 资源
func New(root string) (*Vault, error) {
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	st, err := os.Stat(abs)
	if err != nil || !st.IsDir() {
		return nil, fmt.Errorf("vault path not a dir: %s", abs)
	}
	return &Vault{root: abs}, nil
}

// Root 返回绝对根路径
func (v *Vault) Root() string { return v.root }

// Node 文件树节点（目录或文件）
type Node struct {
	Name     string `json:"name"`
	Path     string `json:"path"` // 相对路径（目录以 / 结尾）
	Type     string `json:"type"` // dir | file
	Ext      string `json:"ext,omitempty"`
	Size     int64  `json:"size,omitempty"`
	Children []Node `json:"children,omitempty"` // dir 的子树
}

// Tree 完整文件树（任意深度、任意格式，排除系统/私密目录）
func (v *Vault) Tree() (*Node, error) {
	root := Node{Name: "", Path: "", Type: "dir", Children: []Node{}}
	err := v.walk("", &root)
	if err != nil {
		return nil, err
	}
	sortChildren(&root)
	return &root, nil
}

func (v *Vault) walk(rel string, parent *Node) error {
	full := filepath.Join(v.root, filepath.FromSlash(rel))
	entries, err := os.ReadDir(full)
	if err != nil {
		return nil // 跳过不可读
	}
	for _, e := range entries {
		name := e.Name()
		// 跳过隐藏项（.开头）
		if strings.HasPrefix(name, ".") {
			continue
		}
		childRel := name
		if rel != "" {
			childRel = rel + "/" + name
		}
		// 排除规则（顶层 + 路径前缀）
		if rel == "" && containsPrefix(ExcludePaths, name) {
			continue
		}
		if e.IsDir() {
			child := Node{Name: name, Path: childRel + "/", Type: "dir", Children: []Node{}}
			if err := v.walk(childRel, &child); err == nil {
				parent.Children = append(parent.Children, child)
			}
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(name), "."))
		parent.Children = append(parent.Children, Node{
			Name: name, Path: childRel, Type: "file",
			Ext: ext, Size: info.Size(),
		})
	}
	return nil
}

func containsPrefix(list []string, name string) bool {
	for _, p := range list {
		if name == p || strings.HasPrefix(name, p+"/") {
			return true
		}
	}
	return false
}

func sortChildren(n *Node) {
	if n.Type != "dir" {
		return
	}
	sort.Slice(n.Children, func(i, j int) bool {
		// 目录在前，文件在后；各自按名排序
		if n.Children[i].Type != n.Children[j].Type {
			return n.Children[i].Type == "dir"
		}
		return strings.ToLower(n.Children[i].Name) < strings.ToLower(n.Children[j].Name)
	})
	for i := range n.Children {
		sortChildren(&n.Children[i])
	}
}

// FileKind 文件内容类型
type FileKind string

const (
	KindText   FileKind = "text"   // 文本可预览（md/txt/json 等）
	KindImage  FileKind = "image"  // 图片（base64 返回）
	KindBinary FileKind = "binary" // 其他（提供下载）
)

// ReadResult 读取结果
type ReadResult struct {
	Path    string `json:"path"`
	Name    string `json:"name"`
	Kind    string `json:"kind"`
	Size    int64  `json:"size"`
	Content string `json:"content,omitempty"` // text
	DataURL string `json:"dataUrl,omitempty"` // image base64
	Ext     string `json:"ext"`
}

var textExts = map[string]bool{
	"md": true, "txt": true, "json": true, "yaml": true, "yml": true,
	"toml": true, "csv": true, "log": true, "html": true, "css": true,
	"js": true, "ts": true, "py": true, "go": true, "java": true,
	"c": true, "cpp": true, "h": true, "sql": true, "xml": true,
	"ini": true, "cfg": true, "conf": true,
}

var imageExts = map[string]string{
	"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
	"gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
	"bmp": "image/bmp", "ico": "image/x-icon",
}

// MaxImageBytes base64 内联预览上限（超过返回 binary）
const MaxImageBytes = 5 * 1024 * 1024

// Read 读取文件：文本/图片/二进制分类处理（安全：拒绝路径逃逸）
func (v *Vault) Read(relPath string) (*ReadResult, error) {
	clean := filepath.Clean(filepath.FromSlash(relPath))
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return nil, fmt.Errorf("path escape rejected: %s", relPath)
	}
	full := filepath.Join(v.root, clean)
	if !strings.HasPrefix(full, v.root) {
		return nil, fmt.Errorf("path escape rejected: %s", relPath)
	}
	info, err := os.Stat(full)
	if err != nil || info.IsDir() {
		return nil, fmt.Errorf("not a file: %s", relPath)
	}
	ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(relPath), "."))
	res := &ReadResult{
		Path: relPath, Name: filepath.Base(relPath),
		Size: info.Size(), Ext: ext,
	}

	switch {
	case textExts[ext]:
		data, err := os.ReadFile(full)
		if err != nil {
			return nil, err
		}
		res.Kind = string(KindText)
		res.Content = string(data)
	case imageExts[ext] != "" && info.Size() <= MaxImageBytes:
		data, err := os.ReadFile(full)
		if err != nil {
			return nil, err
		}
		res.Kind = string(KindImage)
		res.DataURL = "data:" + imageExts[ext] + ";base64," + base64.StdEncoding.EncodeToString(data)
	default:
		res.Kind = string(KindBinary)
	}
	return res, nil
}

// AbsPath 返回绝对路径（供下载端点用；调用方需再校验）
func (v *Vault) AbsPath(relPath string) (string, error) {
	clean := filepath.Clean(filepath.FromSlash(relPath))
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path escape rejected")
	}
	full := filepath.Join(v.root, clean)
	if !strings.HasPrefix(full, v.root) {
		return "", fmt.Errorf("path escape rejected")
	}
	return full, nil
}

// Move 移动文件/目录到目标目录（relFrom → relToDir 内）。同盘 os.Rename。
// 安全：路径校验 + 禁止移到自身子目录内。
func (v *Vault) Move(relFrom, relToDir string) (string, error) {
	fromAbs, err := v.AbsPath(relFrom)
	if err != nil {
		return "", err
	}
	toAbs, err := v.AbsPath(relToDir)
	if err != nil {
		return "", err
	}
	st, err := os.Stat(toAbs)
	if err != nil || !st.IsDir() {
		return "", fmt.Errorf("target not a directory: %s", relToDir)
	}
	// 禁止移动目录到自身/子目录（to 在 from 内部 = 移进自己）
	if fromAbs != toAbs &&
		strings.HasPrefix(toAbs+string(filepath.Separator), fromAbs+string(filepath.Separator)) {
		return "", fmt.Errorf("cannot move into its own subtree")
	}
	name := filepath.Base(fromAbs)
	dest := filepath.Join(toAbs, name)
	// 原地移动（dest == from）幂等返回成功
	if dest == fromAbs {
		return relFrom, nil
	}
	if _, err := os.Stat(dest); err == nil {
		return "", fmt.Errorf("destination exists: %s", filepath.ToSlash(dest))
	}
	if err := os.Rename(fromAbs, dest); err != nil {
		return "", err
	}
	relDest, _ := filepath.Rel(v.root, dest)
	return filepath.ToSlash(relDest), nil
}

// MergeFiles 把两个同目录文件合并进新建文件夹（A、B 移入新夹）。
// 新夹名自动取唯一"新建文件夹"（重名加序号 2/3...）。
func (v *Vault) MergeFiles(aPath, bPath string) (string, error) {
	aAbs, err := v.AbsPath(aPath)
	if err != nil {
		return "", err
	}
	bAbs, err := v.AbsPath(bPath)
	if err != nil {
		return "", err
	}
	if aAbs == bAbs {
		return "", fmt.Errorf("cannot merge a file with itself")
	}
	if filepath.Dir(aAbs) != filepath.Dir(bAbs) {
		return "", fmt.Errorf("files must be in same directory")
	}
	dir := filepath.Dir(aAbs)
	name := uniqueDirName(dir, "新建文件夹")
	newDir := filepath.Join(dir, name)
	if err := os.Mkdir(newDir, 0o755); err != nil {
		return "", err
	}
	for _, abs := range []string{aAbs, bAbs} {
		dest := filepath.Join(newDir, filepath.Base(abs))
		if _, err := os.Stat(dest); err == nil {
			os.RemoveAll(newDir)
			return "", fmt.Errorf("merge conflict")
		}
		if err := os.Rename(abs, dest); err != nil {
			os.RemoveAll(newDir)
			return "", err
		}
	}
	rel, _ := filepath.Rel(v.root, newDir)
	return filepath.ToSlash(rel), nil
}

// CreateDir 在父目录下创建唯一命名目录
func (v *Vault) CreateDir(parentRel, baseName string) (string, error) {
	parentAbs, err := v.AbsPath(parentRel)
	if err != nil {
		return "", err
	}
	st, err := os.Stat(parentAbs)
	if err != nil || !st.IsDir() {
		return "", fmt.Errorf("parent not a directory")
	}
	if baseName == "" {
		baseName = "新建文件夹"
	}
	name := uniqueDirName(parentAbs, baseName)
	if err := os.Mkdir(filepath.Join(parentAbs, name), 0o755); err != nil {
		return "", err
	}
	rel, _ := filepath.Rel(v.root, filepath.Join(parentAbs, name))
	return filepath.ToSlash(rel), nil
}

func uniqueDirName(parentAbs, base string) string {
	candidate := base
	for i := 2; ; i++ {
		if _, err := os.Stat(filepath.Join(parentAbs, candidate)); os.IsNotExist(err) {
			return candidate
		}
		candidate = fmt.Sprintf("%s %d", base, i)
	}
}

// Rename 重命名文件/目录（仅名字，路径不变）。newName 不能含路径分隔符/非法字符。
func (v *Vault) Rename(relPath, newName string) (string, error) {
	oldAbs, err := v.AbsPath(relPath)
	if err != nil {
		return "", err
	}
	newName = strings.TrimSpace(newName)
	if newName == "" || strings.ContainsAny(newName, `/\:*?"<>|`) {
		return "", fmt.Errorf("invalid name: %q", newName)
	}
	dir := filepath.Dir(oldAbs)
	newAbs := filepath.Join(dir, newName)
	if _, err := os.Stat(newAbs); err == nil {
		return "", fmt.Errorf("name exists: %s", newName)
	}
	if err := os.Rename(oldAbs, newAbs); err != nil {
		return "", err
	}
	rel, _ := filepath.Rel(v.root, newAbs)
	return filepath.ToSlash(rel), nil
}

// NewFile 在目录下创建新文件（name 缺省 "未命名"，确保 .md 扩展）
func (v *Vault) NewFile(dirRel, name string) (string, error) {
	dirAbs, err := v.AbsPath(dirRel)
	if err != nil {
		return "", err
	}
	st, err := os.Stat(dirAbs)
	if err != nil || !st.IsDir() {
		return "", fmt.Errorf("dir not found: %s", dirRel)
	}
	name = strings.TrimSpace(name)
	if name == "" {
		name = "未命名"
	}
	if !strings.HasSuffix(strings.ToLower(name), ".md") {
		name += ".md"
	}
	if strings.ContainsAny(name, `/\:*?"<>|`) {
		return "", fmt.Errorf("invalid name")
	}
	full := filepath.Join(dirAbs, uniqueFileName(dirAbs, name))
	if err := os.WriteFile(full, []byte(""), 0o644); err != nil {
		return "", err
	}
	rel, _ := filepath.Rel(v.root, full)
	return filepath.ToSlash(rel), nil
}

func uniqueFileName(dirAbs, name string) string {
	if _, err := os.Stat(filepath.Join(dirAbs, name)); os.IsNotExist(err) {
		return name
	}
	ext := filepath.Ext(name)
	base := strings.TrimSuffix(name, ext)
	for i := 2; ; i++ {
		candidate := fmt.Sprintf("%s %d%s", base, i, ext)
		if _, err := os.Stat(filepath.Join(dirAbs, candidate)); os.IsNotExist(err) {
			return candidate
		}
	}
}

// Trash 软删除：把文件/目录移到 Vault/.trash 下（保留相对结构，重名加时间戳）。
// 不永久删除，可后续提供恢复/清空。
func (v *Vault) Trash(relPath string) (string, error) {
	abs, err := v.AbsPath(relPath)
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(abs); err != nil {
		return "", fmt.Errorf("not found: %s", relPath)
	}
	trashRoot := filepath.Join(v.root, ".trash")
	if err := os.MkdirAll(trashRoot, 0o755); err != nil {
		return "", err
	}
	dest := filepath.Join(trashRoot, filepath.FromSlash(relPath))
	// 目标已存在 → 文件名加时间戳前缀
	if _, err := os.Stat(dest); err == nil {
		ts := time.Now().Format("20060102-150405")
		name := filepath.Base(dest)
		dir := filepath.Dir(dest)
		dest = filepath.Join(dir, ts+"_"+name)
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return "", err
	}
	if err := os.Rename(abs, dest); err != nil {
		return "", err
	}
	rel, _ := filepath.Rel(v.root, dest)
	return filepath.ToSlash(rel), nil
}

// Unfolder 取消文件夹（解散）：把目录内所有内容移到上级目录（重名自动加前缀），
// 然后删除空的文件夹壳。文件不丢失，只是释放到父级。
func (v *Vault) Unfolder(relDir string) (int, error) {
	dirAbs, err := v.AbsPath(relDir)
	if err != nil {
		return 0, err
	}
	st, err := os.Stat(dirAbs)
	if err != nil || !st.IsDir() {
		return 0, fmt.Errorf("not a directory: %s", relDir)
	}
	parentAbs := filepath.Dir(dirAbs)
	if parentAbs == v.root {
		return 0, fmt.Errorf("cannot unfolder root")
	}
	entries, err := os.ReadDir(dirAbs)
	if err != nil {
		return 0, err
	}
	folderName := filepath.Base(dirAbs)
	moved := 0
	for _, e := range entries {
		src := filepath.Join(dirAbs, e.Name())
		dest := filepath.Join(parentAbs, e.Name())
		if _, err := os.Stat(dest); err == nil {
			// 重名：加 "文件夹名_" 前缀（再重加时间戳）
			dest = filepath.Join(parentAbs, folderName+"_"+e.Name())
			if _, err := os.Stat(dest); err == nil {
				dest = filepath.Join(parentAbs,
					fmt.Sprintf("%s_%s_%s", folderName, time.Now().Format("20060102-150405"), e.Name()))
			}
		}
		if err := os.Rename(src, dest); err != nil {
			return moved, err
		}
		moved++
	}
	// 删除空文件夹壳
	if err := os.Remove(dirAbs); err != nil {
		return moved, err
	}
	return moved, nil
}

// TrashList 列出回收站(.trash)内所有文件（平铺，带原位置）。目录按内容展开。
type TrashItem struct {
	Path     string `json:"path"`     // 相对 .trash 的路径（恢复/删除用）
	OrigPath string `json:"origPath"` // 原相对 Vault 路径
	Name     string `json:"name"`
	Size     int64  `json:"size"`
	ModTime  string `json:"modTime"`
}

var tsPrefixRe = regexp.MustCompile(`^\d{8}-\d{6}_`)

func (v *Vault) TrashList() ([]TrashItem, error) {
	trashRoot := filepath.Join(v.root, ".trash")
	items := []TrashItem{}
	err := filepath.Walk(trashRoot, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		rel, _ := filepath.Rel(trashRoot, path)
		rel = filepath.ToSlash(rel)
		origRel := rel
		// 若因重名加了时间戳前缀，还原为原路径（可能仍冲突，恢复时处理）
		base := filepath.Base(rel)
		if tsPrefixRe.MatchString(base) {
			origBase := tsPrefixRe.ReplaceAllString(base, "")
			dir := filepath.Dir(rel)
			origRel = filepath.ToSlash(filepath.Join(dir, origBase))
		}
		items = append(items, TrashItem{
			Path: rel, OrigPath: origRel, Name: info.Name(),
			Size: info.Size(), ModTime: info.ModTime().Format("2006-01-02 15:04"),
		})
		return nil
	})
	return items, err
}

// TrashRestore 从回收站恢复：移到原位置（原位置冲突则自动加序号），父目录自动重建。
func (v *Vault) TrashRestore(trashRel string) (string, error) {
	abs := filepath.Join(v.root, ".trash", filepath.FromSlash(trashRel))
	if _, err := os.Stat(abs); err != nil {
		return "", fmt.Errorf("not in trash: %s", trashRel)
	}
	origRel := trashRel
	base := filepath.Base(trashRel)
	if tsPrefixRe.MatchString(base) {
		origBase := tsPrefixRe.ReplaceAllString(base, "")
		origRel = filepath.ToSlash(filepath.Join(filepath.Dir(trashRel), origBase))
	}
	dest := filepath.Join(v.root, filepath.FromSlash(origRel))
	// 冲突 → 加序号
	if _, err := os.Stat(dest); err == nil {
		ext := filepath.Ext(origRel)
		stem := strings.TrimSuffix(origRel, ext)
		for i := 2; ; i++ {
			candidate := filepath.Join(v.root, filepath.FromSlash(fmt.Sprintf("%s (%d)%s", stem, i, ext)))
			if _, err := os.Stat(candidate); os.IsNotExist(err) {
				dest = candidate
				break
			}
		}
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return "", err
	}
	if err := os.Rename(abs, dest); err != nil {
		return "", err
	}
	rel, _ := filepath.Rel(v.root, dest)
	return filepath.ToSlash(rel), nil
}

// TrashPurge 从回收站永久删除（不可恢复，慎用）
func (v *Vault) TrashPurge(trashRel string) error {
	abs := filepath.Join(v.root, ".trash", filepath.FromSlash(trashRel))
	if _, err := os.Stat(abs); err != nil {
		return fmt.Errorf("not in trash: %s", trashRel)
	}
	return os.RemoveAll(abs)
}

// TrashEmpty 清空回收站
func (v *Vault) TrashEmpty() error {
	trashRoot := filepath.Join(v.root, ".trash")
	entries, err := os.ReadDir(trashRoot)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for _, e := range entries {
		if err := os.RemoveAll(filepath.Join(trashRoot, e.Name())); err != nil {
			return err
		}
	}
	return nil
}
