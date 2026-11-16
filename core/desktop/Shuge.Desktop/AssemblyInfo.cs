using System.Runtime.InteropServices;

// 程序集级 COM 可见（ShugeHost 要经 WebView2 的 AddHostObjectToScript 暴露给页面）。
// 少这一行的话，即使类上写了 [ComVisible(true)]，桥也注册不上 ——
// 页面里拿到的 hostObjects 里会没有 shuge，报「找不到元素 (0x80070490)」（2026-10-11 实测）。
[assembly: ComVisible(true)]
