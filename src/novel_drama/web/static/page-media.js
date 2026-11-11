/* 音效 / 配图 / 视频位页。
 *
 * 条目来自导演标注物化；「运行」调 `media sfx|image|video run`，
 * 离线渲染器会真出文件（ffmpeg 合成音效与视频、Pillow 画图），状态写回 asset。
 *
 * 本页只做三列清单；点某一行把条目交给右栏 Inspector（`NWInspector.show('media', …)`）
 * 看提示词、时长、状态并内联播放，右栏「重跑此项」仍走同一个 `/api/ui/media/run`。
 */

(function () {
  let bound = false;
  let busy = false;
  let rowsById = new Map(); // `${kind}:${id}` → 媒体条目，供右栏取原始字段
  let selected = "";

  const el = (id) => document.getElementById(id);

  /* 单个条目的就地预览：配图给缩略图（点击放大）、视频给内联播放器、音效给播放条。
   * src 统一走只读文件接口（uiFileUrl），相对路径取接口返回的 asset_id。
   * 参数：item 媒体条目；kind 媒体类型（sfx / image / video）。
   * 返回：HTML 字符串。
   */
  function previewHtml(item, kind) {
    const rel = item.asset_id || "";
    if (!rel) return "<span class='muted'>无产物</span>";
    const url = uiFileUrl(rel);
    if (!url) return `<span class="error-box">缺少工程根目录，无法构造预览地址：${esc(rel)}</span>`;
    if (kind === "image") {
      return `<img class="md-thumb" loading="lazy" alt="${esc(item.prompt || "")}" data-preview data-rel="${esc(rel)}" src="${esc(url)}" />`;
    }
    if (kind === "video") {
      return `<video class="md-video" preload="metadata" controls data-preview data-rel="${esc(rel)}" src="${esc(url)}"></video>`;
    }
    return `<audio class="md-audio" preload="none" controls data-preview data-rel="${esc(rel)}" src="${esc(url)}"></audio>`;
  }

  /* 渲染一张媒体表；行上带 kind / id，便于点选。
   * 参数：rows 条目数组；kind 媒体类型（sfx / image / video）。
   * 返回：HTML 字符串。
   */
  function table(rows, kind) {
    if (!rows.length) return "<p class='muted'>暂无条目（先做导演标注并应用）。</p>";
    const head =
      kind === "sfx"
        ? "<tr><th>#</th><th>类型</th><th>预览</th><th>提示词</th><th>状态</th><th>时长</th><th>产物 / 错误</th></tr>"
        : "<tr><th>#</th><th>模式</th><th>模板</th><th>画幅</th><th>宽高</th><th>参考图</th><th>预览</th><th>提示词</th><th>状态</th><th>时长</th><th>产物 / 错误</th></tr>";
    const body = rows
      .map((item) => {
        const extra = item.error
          ? `<span class="err">${esc(typeof item.error === "string" ? item.error : item.error.message || JSON.stringify(item.error))}</span>`
          : esc(item.asset_id || "—");
        const kindText = kind === "sfx" ? item.kind : item.mode || "—";
        // 图像/视频多三列只读信息：模板、画幅、宽高（后端解析失败时给空值）
        const specCells =
          kind === "sfx"
            ? ""
            : `<td>${esc(item.template || "—")}</td>` +
              `<td>${esc(item.aspect_ratio || "—")}</td>` +
              `<td>${item.width && item.height ? `${esc(item.width)}×${esc(item.height)}` : "—"}</td>`;
        // 参考图列：关键图显示「N 张（名称 / 名称）」，缺图告警逐条用小字提示
        const refs = Array.isArray(item.references) ? item.references : [];
        const refWarnings = Array.isArray(item.reference_warnings) ? item.reference_warnings : [];
        const refNames = refs.map((ref) => ref.name || ref.id || "").filter(Boolean);
        const refText = refs.length ? `${refs.length} 张${refNames.length ? `（${refNames.join(" / ")}）` : ""}` : "—";
        const refCell =
          kind === "sfx"
            ? ""
            : `<td class="wrap">${esc(refText)}${refWarnings.map((text) => `<br><span class="err">${esc(text)}</span>`).join("")}</td>`;
        const key = `${kind}:${item.id}`;
        rowsById.set(key, { kind, row: item });
        const active = selected === key ? "is-selected" : "";
        return (
          `<tr class="${active}" data-media="true" data-media-key="${esc(key)}">` +
          `<td>${esc(item.index ?? "")}</td><td>${esc(kindText || "")}</td>${specCells}${refCell}` +
          `<td class="md-preview">${previewHtml(item, kind)}</td>` +
          `<td class="wrap">${esc(item.prompt || "")}</td>` +
          `<td><span class="state-badge s-${esc(item.status)}"><span class="dot"></span>${esc(item.status)}</span></td>` +
          `<td>${msText(item.duration_ms)}</td><td class="wrap">${extra}</td></tr>`
        );
      })
      .join("");
    return `<table class="grid"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  /* 预览元素加载失败时，把控件换成「原始路径 + 失败原因」，不留空白。
   * 参数：box 容器（某张媒体表）。返回：无。
   */
  function bindPreviewErrors(box) {
    box.querySelectorAll("[data-preview]").forEach((node) => {
      node.addEventListener("error", () => {
        const rel = node.dataset.rel || "";
        const fallback = document.createElement("div");
        fallback.className = "error-box";
        fallback.textContent = `加载失败：${rel}（文件不存在或浏览器不支持该容器）`;
        if (node.parentElement) node.parentElement.replaceChild(fallback, node);
      });
    });
    // 配图缩略图点击放大
    box.querySelectorAll("img[data-preview]").forEach((node) => {
      node.addEventListener("click", (event) => {
        event.stopPropagation();
        openLightbox(node.getAttribute("src"), node.dataset.rel || "");
      });
    });
  }

  /* 配图放大弹层（懒创建，点遮罩或按 Esc 关闭）。
   * 参数：url 图片地址；rel 相对路径（作说明文字）。
   * 返回：无。
   */
  function openLightbox(url, rel) {
    let layer = el("mediaLightbox");
    if (!layer) {
      layer = document.createElement("div");
      layer.id = "mediaLightbox";
      layer.className = "lightbox";
      layer.innerHTML = "<figure><img alt='' /><figcaption class='muted'></figcaption></figure>";
      layer.addEventListener("click", () => { layer.hidden = true; });
      document.body.appendChild(layer);
    }
    const img = layer.querySelector("img");
    const caption = layer.querySelector("figcaption");
    if (img) img.src = url || "";
    if (caption) caption.textContent = rel;
    layer.hidden = false;
  }

  /* 拉取并渲染三类媒体。 */
  async function load() {
    rowsById = new Map();
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
      ["mdSfx", "mdImage", "mdVideo"].forEach((id) => bindPreviewErrors(el(id)));
    } catch (error) {
      NW.toast(`读取媒体失败：${error.message}`);
    }
  }

  /* 执行一类媒体生成。参数：kind 媒体类型。返回：Promise，无。 */
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
        busy: `正在生成${({ sfx: "音效", image: "配图", video: "视频位" })[kind] || kind}…`,
        busyDetail: "媒体生成较慢，请勿关闭页面",
      });
      const text = `${kind}：就绪 ${result.ready} / 跳过 ${result.skipped} / 失败 ${result.failed}`;
      NW.toast(text, result.failed === 0);
      await load();
      if (window.NWInspector) NWInspector.refreshMedia();
    } catch (error) {
      NW.toast(`执行失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el(button).disabled = false;
    }
  }

  /* 点选一行：记住选中并交给右栏。参数：event 点击事件。返回：无。 */
  function onRowClick(event) {
    const row = event.target.closest("tr[data-media-key]");
    if (!row) return;
    const key = row.dataset.mediaKey;
    const found = rowsById.get(key);
    if (!found) return;
    selected = key;
    el("mdSfx").querySelectorAll("tr.is-selected").forEach((node) => node.classList.remove("is-selected"));
    el("mdImage").querySelectorAll("tr.is-selected").forEach((node) => node.classList.remove("is-selected"));
    el("mdVideo").querySelectorAll("tr.is-selected").forEach((node) => node.classList.remove("is-selected"));
    row.classList.add("is-selected");
    if (window.NWInspector) NWInspector.show("media", found);
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("mdRunSfx").addEventListener("click", () => run("sfx"));
    el("mdRunImage").addEventListener("click", () => run("image"));
    el("mdRunVideo").addEventListener("click", () => run("video"));
    ["mdSfx", "mdImage", "mdVideo"].forEach((id) => el(id).addEventListener("click", onRowClick));
    // Esc 关闭配图放大层
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const layer = el("mediaLightbox");
      if (layer && !layer.hidden) layer.hidden = true;
    });
    NW.onChapter(() => load());
  }

  window.Pages.media = {
    activate() {
      bind();
      load();
    },
    /* 供右栏「重跑此项」触发同一套运行逻辑。参数：kind 媒体类型。返回：Promise，无。 */
    run,
    /* 供右栏在重跑后刷新清单。返回：Promise，无。 */
    load,
    /* 按 kind + id 取当前清单里的条目（重跑后右栏据此重画）。
     * 参数：kind 媒体类型；id 条目 ID。返回：{ kind, row } 或 null。 */
    find(kind, id) {
      return rowsById.get(`${kind}:${id}`) || null;
    },
  };
})();
