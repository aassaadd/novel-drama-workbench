/* 通用模态编辑器：全局单例的大号文本输入浮层。
 *
 * 只负责「开一个能写长文本的窗口」，不读任何页面状态：
 *   - NWEditor.open(options)   通用多行编辑器（标题 / 提示 / 字数统计可选）
 *   - NWEditor.openBrief(options) 「生成需求输入」薄封装
 *   - 「保存」或 ⌘/Ctrl+Enter 提交并关闭；「取消」、Esc、点遮罩关闭且不回调
 * 业务侧只从 onSave(text) 取结果，落盘与刷新仍由调用方自己处理。
 */

(function () {
  let mask = null;          // 当前遮罩节点（单例，同时只允许一个）
  let saveHandler = null;   // 当前编辑器的保存回调
  let previousFocus = null; // 打开前的焦点元素，关闭后还回去

  /* 关闭当前模态：移除 DOM、解绑键盘监听、恢复打开前的焦点。
   * 参数：无。
   * 返回：无。
   */
  function close() {
    if (!mask) return;
    document.removeEventListener("keydown", onKeydown, true);
    mask.remove();
    mask = null;
    saveHandler = null;
    const target = previousFocus;
    previousFocus = null;
    if (target && document.contains(target) && typeof target.focus === "function") {
      target.focus({ preventScroll: true });
    }
  }

  /* 提交：读出文本、先关闭浮层，再把文本交给调用方的 onSave。
   * 参数：无。
   * 返回：无。
   */
  function submit() {
    if (!mask) return;
    const textarea = mask.querySelector(".modal-input");
    const text = textarea ? textarea.value : "";
    const handler = saveHandler;
    close();
    if (typeof handler === "function") handler(text);
  }

  /* 全局键盘处理：Esc 关闭（不回调），⌘/Ctrl+Enter 提交。
   * 参数：event 键盘事件。
   * 返回：无。
   */
  function onKeydown(event) {
    if (!mask) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      submit();
    }
  }

  /* 创建一个小元素并挂到父节点。
   * 参数：tag 标签名；className 类名（可空）；text 文本（可空）；parent 父节点。
   * 返回：新建的元素。
   */
  function node(tag, className, text, parent) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = text;
    if (parent) parent.appendChild(item);
    return item;
  }

  /* 打开通用多行编辑器（重复调用会先关掉上一个，保证单例）。
   * 参数：options 配置对象
   *   - title 标题（可空）
   *   - value 初始文本
   *   - placeholder 占位提示
   *   - maxLength 字数上限（给了才显示「当前/上限」并限制输入）
   *   - hint 输入框下方的说明文字
   *   - submitText / cancelText 按钮文案，默认「保存」/「取消」
   *   - onSave(text) 保存回调，关闭后才调用
   * 返回：无。
   */
  function open(options) {
    const opts = options || {};
    close();
    previousFocus = document.activeElement;

    const limit = Number(opts.maxLength) > 0 ? Math.floor(Number(opts.maxLength)) : 0;

    mask = document.createElement("div");
    mask.className = "modal-mask";

    const dialog = node("div", "modal-editor", null, mask);
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    if (opts.title) node("h3", "modal-title", opts.title, dialog);

    const textarea = node("textarea", "modal-input", null, dialog);
    textarea.value = opts.value === undefined || opts.value === null ? "" : String(opts.value);
    textarea.placeholder = opts.placeholder || "";
    if (limit) textarea.maxLength = limit;

    if (opts.hint) node("p", "modal-hint", opts.hint, dialog);

    const foot = node("div", "modal-foot", null, dialog);
    let count = null;
    if (limit) {
      count = node("span", "modal-count", `0/${limit}`, foot);

      /* 同步字数统计。
       * 参数：无。
       * 返回：无。
       */
      const syncCount = () => {
        count.textContent = `${textarea.value.length}/${limit}`;
      };
      textarea.addEventListener("input", syncCount);
      syncCount();
    }

    const cancel = node("button", "btn", opts.cancelText || "取消", foot);
    cancel.type = "button";
    cancel.addEventListener("click", close);

    const save = node("button", "btn primary", opts.submitText || "保存", foot);
    save.type = "button";
    save.addEventListener("click", submit);

    // 点遮罩（不是弹窗内部）关闭且不回调
    mask.addEventListener("click", (event) => {
      if (event.target === mask) close();
    });

    saveHandler = typeof opts.onSave === "function" ? opts.onSave : null;
    document.body.appendChild(mask);
    document.addEventListener("keydown", onKeydown, true);

    textarea.focus();
    const end = textarea.value.length;
    if (typeof textarea.setSelectionRange === "function") textarea.setSelectionRange(end, end);
  }

  /* 「生成需求输入」薄封装：标题 + 多行文本，允许为空提交，交给调用方判断。
   * 需求按标题缓存在 localStorage，下次点开自动带出上次填写的内容。
   * 参数：options {title, placeholder, value, onSave}
   * 返回：无。
   */
  function openBrief(options) {
    const opts = options || {};
    const title = opts.title || "生成需求";
    const cacheKey = `ndw.brief.${title}`;
    let cached = "";
    if (opts.value === undefined) {
      try {
        cached = localStorage.getItem(cacheKey) || "";
      } catch (error) {
        // 隐私模式下 localStorage 不可用，退化为不带缓存
      }
    }
    open({
      title,
      value: opts.value === undefined ? cached : opts.value,
      placeholder: opts.placeholder || "",
      hint: cached ? "已带出上次填写的需求，可直接修改后重新生成" : "",
      submitText: "生成",
      onSave(text) {
        try {
          localStorage.setItem(cacheKey, String(text || ""));
        } catch (error) {
          // 写入失败不影响本次生成
        }
        if (typeof opts.onSave === "function") opts.onSave(text);
      },
    });
  }

  window.NWEditor = { open, openBrief };
})();
