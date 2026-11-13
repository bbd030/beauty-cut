"use strict";
(() => {
  const data = JSON.parse(document.getElementById("report-data").textContent);
  const video = document.getElementById("player");
  const input = document.getElementById("source-file");
  const status = document.getElementById("player-status");
  const buttons = Array.from(document.querySelectorAll("button[data-start]"));
  let url = null, ready = false, stopAt = null, requested = null;
  const say = message => { status.textContent = message; };
  const disable = () => { ready = false; stopAt = requested = null; buttons.forEach(b => b.disabled = true); };
  input.addEventListener("change", () => {
    disable(); video.pause(); video.removeAttribute("src"); video.load();
    if (url) { URL.revokeObjectURL(url); url = null; }
    const file = input.files[0];
    if (!file) { say("尚未选择原视频。"); return; }
    if (file.name !== data.source.name || file.size !== data.source.size_bytes) {
      say("文件名或大小与生成报告时不符，请选择对应原视频。"); return;
    }
    say("正在读取本地原视频；文件名和大小核对不构成内容一致性证明。");
    url = URL.createObjectURL(file); video.src = url; video.load();
  });
  video.addEventListener("loadedmetadata", () => {
    const offset = data.playback.offset_seconds;
    if (!Number.isFinite(video.duration) || video.duration + 0.25 < data.video_duration) {
      disable(); say("浏览器读取到的时长与报告不符，已停用镜头定位，请检查文件。"); return;
    }
    if (offset === null || data.playback.multiple_video_streams) {
      disable(); say("无法可靠建立此文件的播放器时间对应关系。可手动播放，逐帧核查请使用边界图片。"); return;
    }
    if (offset + data.video_duration > video.duration + 0.25) {
      disable(); say("播放器时间范围与检测时间不符，已停用镜头定位；仍可手动播放和查看图片。"); return;
    }
    ready = true; buttons.forEach(b => b.disabled = false);
    say("已加载本地视频。可点击镜头近似定位；请对照边界图片确认，浏览器可能存在时间偏移。");
  });
  video.addEventListener("error", () => {
    if (!url) return;
    disable(); say("浏览器无法读取或播放此视频编码。请使用本地播放器核对时间，报告图片仍可查看。");
  });
  buttons.forEach(button => button.addEventListener("click", () => {
    if (!ready) return;
    video.pause();
    requested = Number(button.dataset.start) + data.playback.offset_seconds;
    stopAt = Number(button.dataset.end) + data.playback.offset_seconds;
    say("正在请求近似定位到镜头开始；逐帧位置以边界图片为准。");
    try {
      video.currentTime = requested;
      video.play().catch(() => { stopAt = null; say("自动播放未完成，请点击播放器播放按钮；边界图片仍可查看。"); });
      video.scrollIntoView({behavior: "smooth", block: "center"});
    } catch (error) { stopAt = requested = null; say("播放器定位失败，请手动拖动或使用边界图片。"); }
  }));
  video.addEventListener("seeked", () => {
    if (requested === null) return;
    const target = requested; requested = null;
    if (Math.abs(video.currentTime - target) > 0.25) {
      stopAt = null; video.pause(); say("浏览器未到达请求位置，请手动定位并对照边界图片。");
    } else { say("已请求镜头播放，起止位置为近似值；可用控制条连续观看全片。逐帧核查以图片为准。"); }
  });
  video.addEventListener("timeupdate", () => {
    if (stopAt !== null && requested === null && video.currentTime >= stopAt) {
      video.pause(); stopAt = null;
    }
  });
  video.addEventListener("pointerdown", () => { stopAt = requested = null; });
  video.addEventListener("seeking", () => { if (requested === null) stopAt = null; });
  const reveal = () => {
    const target = document.getElementById(location.hash.slice(1));
    if (target && target.tagName === "DETAILS") { target.open = true; target.scrollIntoView(); }
  };
  window.addEventListener("hashchange", reveal); reveal();
  window.addEventListener("pagehide", () => { if (url) URL.revokeObjectURL(url); });
})();
