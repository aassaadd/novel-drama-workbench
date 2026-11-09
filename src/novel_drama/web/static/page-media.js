/* 音效 / 配图 / 视频位页。
 *
 * 条目来自导演标注物化；「运行」调 `media sfx|image|video run`，
 * 离线渲染器会真出文件（ffmpeg 合成音效与视频、Pillow 画图），状态写回 asset。
 */

(function () {
  let bound = false;
  let busy = false;

  const el = (id) => document.getElementById(id);

  /* 渲染一张媒体表。 */
  function table(rows, kind) {
    if (!rows.length) return "<p class='muted'>暂无条目（先做导演标注并应用）。</p>";
    const head =
      kind === "sfx"
        ? "<tr><th>#</th><th>类型</th><th>提示词</th><th>状态</th><th>时长</th><th>产物 / 错误</th></tr>"
        : "<tr><th>#</th><th>模式</th><th>提示词</th><th>状态</th><th>时长</th><th>产物 / 错误</th></tr>";
    const body = rows
      .map((item) => {
        const extra = item.error
          ? `<span class="err">${esc(typeof item.error === "string" ? item.error : item.error.message || JSON.stringify(item.error))}</span>`
          : esc(item.asset_id || "—");
        const kindText = kind === "sfx" ? item.kind : item.mode || "—";
        return (
          `<tr><td>${esc(item.index ?? "")}</td><td>${esc(kindText || "")}</td>` +
          `<td class="wrap">${esc(item.prompt || "")}</td>` +
          `<td><span class="state-badge s-${esc(item.status)}"><span class="dot"></span>${esc(item.status)}</span></td>` +
          `<td>${msText(item.duration_ms)}</td><td class="wrap">${extra}</td></tr>`
        );
      })
      .join("");
    return `<table class="grid"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  /* 拉取并渲染三类媒体。 */
  async function load() {
    if (!NW.state.path || !NW.state.chapter) {
      ["mdSfx", "mdImage", "mdVideo"].forEach((id) => {
        el(id).innerHTML = "<p class='muted'>先打开工程并选择章节。</p>";
      });
      return;
    }
    try {
      const data = await NW.api(`/api/ui/media?${NW.qs({ path: NW.state.path, chapter: NW.state.chapter })}`);
      el("mdSfx").innerHTML = table(data.sfx || [], "sfx");
      el("mdImage").innerHTML = table(data.image || [], "image");
      el("mdVideo").innerHTML = table(data.video || [], "video");
    } catch (error) {
      NW.toast(`读取媒体失败：${error.message}`);
    }
  }

  /* 执行一类媒体生成。 */
  async function run(kind) {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    const button = { sfx: "mdRunSfx", image: "mdRunImage", video: "mdRunVideo" }[kind];
    el(button).disabled = true;
    try {
      const result = await NW.api("/api/ui/media/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, kind, force: el("mdForce").checked }),
      });
      const text = `${kind}：就绪 ${result.ready} / 跳过 ${result.skipped} / 失败 ${result.failed}`;
      NW.toast(text, result.failed === 0);
      await load();
    } catch (error) {
      NW.toast(`执行失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el(button).disabled = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("mdRunSfx").addEventListener("click", () => run("sfx"));
    el("mdRunImage").addEventListener("click", () => run("image"));
    el("mdRunVideo").addEventListener("click", () => run("video"));
    NW.onChapter(() => load());
  }

  window.Pages.media = {
    activate() {
      bind();
      load();
    },
  };
})();
