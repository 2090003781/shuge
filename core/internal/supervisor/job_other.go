//go:build !windows

package supervisor

import (
	"fmt"
	"os"
)

// 非 Windows 平台没有 Job Object：空实现（调用方只看失败信号，不影响主流程）
type jobObject struct{}

func newJobObject() (*jobObject, error) { return &jobObject{}, nil }

func (j *jobObject) assign(p *os.Process) error {
	return fmt.Errorf("Job Object 仅 Windows 支持")
}

func (j *jobObject) Close() error { return nil }
