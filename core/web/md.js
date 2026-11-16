// 轻量 Markdown → HTML 渲染器（本地无依赖，够笔记查看用）
// 支持：标题/段落/列表/代码块/行内代码/引用/粗体/链接/表格/分割线
(function () {
  function esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function inline(s) {
    s = esc(s);
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, function (_, t, alias) {
      return '<a class="wiki" data-target="' + t.trim() + '">' + (alias || t.trim()) + '</a>';
    });
    s = s.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    return s;
  }

  function render(src) {
    const lines = src.split('\n');
    const out = [];
    let i = 0;
    let listStack = [];

    function closeLists(depth) {
      while (listStack.length > depth) {
        out.push('</' + listStack.pop() + '>');
      }
    }
    function openList(depth, type) {
      while (listStack.length < depth) { listStack.push(type); }
      while (listStack.length > depth) { out.push('</' + listStack.pop() + '>'); }
    }

    while (i < lines.length) {
      const line = lines[i];

      // 代码块
      if (/^```/.test(line)) {
        closeLists(0);
        const lang = line.slice(3).trim();
        const code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++; // 跳过结束 ```
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }

      // 标题
      let m = line.match(/^(#{1,6})\s+(.*)$/);
      if (m) {
        closeLists(0);
        const lv = m[1].length;
        out.push('<h' + lv + '>' + inline(m[2]) + '</h' + lv + '>');
        i++; continue;
      }

      // 分割线
      if (/^---+$/.test(line) || /^\*\*\*+$/.test(line)) {
        closeLists(0); out.push('<hr>'); i++; continue;
      }

      // 引用
      if (/^>\s?/.test(line)) {
        closeLists(0);
        const quote = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { quote.push(lines[i].replace(/^>\s?/, '')); i++; }
        out.push('<blockquote>' + inline(quote.join('\n')) + '</blockquote>');
        continue;
      }

      // 列表
      m = line.match(/^(\s*)([-*]|\d+\.)\s+(.*)$/);
      if (m) {
        const depth = Math.floor(m[1].length / 2) + 1;
        const type = /^\d/.test(m[2]) ? 'ol' : 'ul';
        openList(depth, type);
        if (depth === listStack.length && out.length && !out[out.length - 1].startsWith('<li')) {
          // noop
        }
        out.push('<li>' + inline(m[3]));
        // 吸收后续续行（非列表非标题）
        while (i + 1 < lines.length && lines[i + 1].trim() && !/^(\s*)([-*]|\d+\.)\s+/.test(lines[i + 1]) && !/^#{1,6}\s/.test(lines[i + 1]) && lines[i + 1].trim() !== '' && !/^```/.test(lines[i + 1])) {
          i++; out.push(' ' + inline(lines[i].trim()));
        }
        out.push('</li>');
        i++; continue;
      }
      closeLists(0);

      // 表格
      if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|/.test(lines[i + 1])) {
        const head = line.split('|').map(s => s.trim()).filter(s => s);
        i += 2; // 跳表头和分隔行
        out.push('<table><thead><tr>' + head.map(h => '<th>' + inline(h) + '</th>').join('') + '</tr></thead><tbody>');
        while (i < lines.length && lines[i].includes('|')) {
          const cells = lines[i].split('|').map(s => s.trim()).filter(s => s);
          out.push('<tr>' + cells.map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>');
          i++;
        }
        out.push('</tbody></table>');
        continue;
      }

      // 空行
      if (!line.trim()) { i++; continue; }

      // 段落
      const para = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !/^#{1,6}\s/.test(lines[i]) && !/^```/.test(lines[i]) && !/^>\s?/.test(lines[i]) && !/^(\s*)([-*]|\d+\.)\s+/.test(lines[i]) && !lines[i].includes('|') && !/^---+$/.test(lines[i])) {
        para.push(lines[i]); i++;
      }
      out.push('<p>' + inline(para.join(' ')) + '</p>');
    }
    closeLists(0);
    return out.join('\n');
  }

  window.mdRender = render;
})();
