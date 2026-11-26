/* PapiClips editor: a full-screen editor for one clip.

   The preview is live: the clip's light preview copy plays in a hidden <video> and is drawn into a 9:16 canvas the
   way the finished clip will look (layout, facecam, colour look, punch-ins, transitions, the big text and the
   captions), so every change shows at once, without waiting for a render. Export sends the edit to the server,
   which makes the real clip with ffmpeg (studio/edit.py, studio/render.py).

   Times are in the clip's source timeline (the same seconds as the clip's start and end); the preview copy starts
   at data.p0. Uses the app's own helpers (api, send, toast, esc, ic, S, jobOf, tick, openCamFix, D).
*/
(function () {
  "use strict";
  const W = 1080, H = 1920;
  const FILLER = /^(u+m+|u+h+|u+h+m+|h+m+|e+r+m+|e+r+|a+h+|m+h*m+|like|basically|actually)[.,!?…]*$/i;
  const FILLER_SAFE = /^(u+m+|u+h+|u+h+m+|h+m+|e+r+m+|e+r+|a+h+|m+h*m+)[.,!?…]*$/i;     // removed by "Remove filler words"
  const TR_ICON = {flash: "✦", whoosh: "≋", zoom: "⤢", wipe: "▶", glitch: "▚", fade: "◐", spin: "↻", blur: "◌", shake: "≈"};
  const TR_HINT = {flash: "A white flash on the cut, with a boom", whoosh: "A fast motion-blur swipe", zoom: "Punches through the cut",
    wipe: "A crimson accent panel sweeps across", glitch: "RGB split and noise", fade: "Dips to black and back", spin: "A quick twist",
    blur: "Blurs out and back in", shake: "Camera shake on the hit"};
  const SFX_ICON = {whoosh: "≋", swoosh: "〰", boom: "✹", hit: "✶", bass: "▼", riser: "↗", ding: "♪", pop: "●", glitch: "▚", shutter: "◉"};
  const css = `
#ed{position:fixed;inset:0;z-index:60;background:#070409;color:#F8FAFC;display:grid;grid-template-rows:58px minmax(0,1fr) var(--tlh,250px);
  font:13px/1.45 "Geist","Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;user-select:none;--lime:#FF2E54;--ink:#FFFFFF;
  --p1:rgba(14,7,13,.56);--p2:rgba(24,12,20,.54);--p3:rgba(44,20,32,.64);--ln:rgba(255,255,255,.15);--ln2:rgba(255,255,255,.24);--dim:#C6BECB;--faint:#8A7E90}
#ed::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;background:linear-gradient(135deg,rgba(8,4,10,.26),rgba(12,5,11,.18)),url('/brand/bg-choso.jpg?v=choso1') center 22%/cover no-repeat;filter:saturate(1.10) contrast(1.06) brightness(.96)}
#ed *{box-sizing:border-box}:where(#ed) button{font:inherit;color:inherit;cursor:pointer}:where(#ed) input,:where(#ed) textarea,:where(#ed) select{font:inherit;user-select:text}
:where(#ed) svg{width:16px;height:16px;fill:currentColor;flex:none}
#toasts{z-index:200!important}
.ed-top{display:flex;align-items:center;gap:10px;padding:0 14px;border-bottom:1px solid var(--ln);background:var(--p1);backdrop-filter:blur(28px) saturate(185%);-webkit-backdrop-filter:blur(28px) saturate(185%);box-shadow:inset 0 1px 0 rgba(255,255,255,.16);min-width:0}
.ed-brand{width:30px;height:30px;border-radius:9px;object-fit:cover;border:1.5px solid rgba(255,255,255,.35);box-shadow:0 3px 12px rgba(255,46,84,.45);flex:none}
.ed-ib{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;border:1px solid var(--ln);background:rgba(255,255,255,.06);color:var(--dim)}
.ed-ib:hover:not(:disabled){color:#fff;background:rgba(255,255,255,.14);border-color:var(--ln2)}.ed-ib:disabled{opacity:.35;cursor:default}
.ed-ttl{min-width:0;flex:0 1 auto}.ed-ttl b{display:block;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:34vw}
.ed-ttl span{font-size:11.5px;color:var(--dim);white-space:nowrap}
.ed-state{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim);white-space:nowrap}.ed-state i{width:7px;height:7px;border-radius:50%;background:#3DDC84}
.ed-state.dirty i{background:#FFB86B}.ed-state.busy i{background:var(--lime);animation:edp 1s infinite}
@keyframes edp{50%{opacity:.3}}
.ed-sp{flex:1}
.ed-btn{height:36px;padding:0 14px;border-radius:999px;display:inline-flex;align-items:center;gap:8px;font-weight:600;font-size:13px;border:1px solid var(--ln2);background:rgba(255,255,255,.08);backdrop-filter:blur(12px);box-shadow:inset 0 1px 0 rgba(255,255,255,.2);white-space:nowrap}
.ed-btn:hover{background:rgba(255,255,255,.15)}.ed-btn.p{background:linear-gradient(135deg,#FF385E,#D6123A);border-color:rgba(255,165,185,.45);color:#fff;box-shadow:inset 0 1px 0 rgba(255,255,255,.35),0 6px 20px rgba(255,46,84,.42)}
.ed-btn.p:hover{background:linear-gradient(135deg,#FF4E70,#EE1D47)}.ed-btn.ai{background:linear-gradient(90deg,rgba(255,46,84,.24),rgba(139,92,246,.18));border-color:rgba(255,110,140,.45);color:#FFD6DF}
.ed-btn:disabled{opacity:.5;cursor:default}.ed-btn.sm{height:30px;padding:0 10px;font-size:12px;border-radius:999px}
.ed-mid{display:grid;grid-template-columns:72px 300px minmax(0,1fr) 284px;min-height:0}
.ed-rail{background:var(--p1);backdrop-filter:blur(26px) saturate(180%);-webkit-backdrop-filter:blur(26px) saturate(180%);border-right:1px solid var(--ln);display:flex;flex-direction:column;gap:3px;padding:8px 6px;overflow:auto}
.ed-rail button{display:flex;flex-direction:column;align-items:center;gap:4px;padding:9px 0;border-radius:12px;border:1px solid transparent;background:transparent;color:var(--dim);font-size:10.5px;font-weight:600}
.ed-rail button svg{width:20px;height:20px}.ed-rail button:hover{color:#fff;background:rgba(255,255,255,.08)}
.ed-rail button.on{background:rgba(255,46,84,.22);border-color:rgba(255,105,135,.4);box-shadow:inset 0 1px 0 rgba(255,255,255,.22);color:#fff}.ed-rail .ai{margin-top:auto;color:#FF8BA3}
.ed-panel,.ed-insp{background:var(--p1);backdrop-filter:blur(26px) saturate(180%);-webkit-backdrop-filter:blur(26px) saturate(180%);padding:14px;overflow:auto;min-height:0}.ed-panel{border-right:1px solid var(--ln)}.ed-insp{border-left:1px solid var(--ln)}
.ed-h{margin:2px 0 10px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);font-weight:700;display:flex;justify-content:space-between;align-items:center;gap:8px}
.ed-h em{font-style:normal;color:#FF7A95;letter-spacing:0;text-transform:none;font-weight:600}
.ed-sec{margin-bottom:20px}
.ed-help{color:var(--dim);font-size:12px;margin:-4px 0 10px}
.ed-grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.ed-grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.ed-tile{border-radius:13px;background:var(--p2);border:1px solid var(--ln);box-shadow:inset 0 1px 0 rgba(255,255,255,.12);padding:7px 6px 7px;text-align:center;font-size:11.5px;font-weight:600;color:var(--dim);position:relative}
.ed-tile:hover{border-color:var(--ln2);color:#fff}.ed-tile.on{border-color:var(--lime);color:#fff;box-shadow:inset 0 0 0 1px var(--lime),0 4px 14px rgba(255,46,84,.25)}
.ed-tile .pv{height:48px;border-radius:8px;margin-bottom:6px;position:relative;overflow:hidden;background:#000 center/cover}
.ed-tile .pv b{position:absolute;inset:0;display:grid;place-items:center;font-size:20px;color:#fff;text-shadow:0 2px 8px #000}
.pv.flash{background:linear-gradient(90deg,#2a1620 0 38%,#fff 50%,#3b1828 62%)}.pv.whoosh{background:repeating-linear-gradient(90deg,#2a1620 0 6px,#4e2234 6px 9px);filter:blur(1.2px)}
.pv.zoom{background:radial-gradient(circle,#ffb3c1 0 10%,#2a1620 60%)}.pv.wipe{background:linear-gradient(110deg,#FF2E54 0 48%,#2a1620 49%)}
.pv.glitch{background:linear-gradient(#2a1620 0 30%,#ff2f6a 30% 36%,#2a1620 36% 60%,#2fd7ff 60% 66%,#2a1620 66%)}.pv.fade{background:linear-gradient(90deg,#000,#4e2234)}
.pv.spin{background:conic-gradient(from 30deg,#2a1620,#69263e,#2a1620)}.pv.blur{background:#4e2234;filter:blur(3px)}.pv.shake{background:linear-gradient(95deg,#2a1620 0 45%,#69263e 45% 47%,#2a1620 47%)}
.ed-chips{display:flex;flex-wrap:wrap;gap:6px}
.ed-chip{display:inline-flex;align-items:center;gap:6px;padding:0 4px 0 10px;height:32px;border-radius:999px;background:var(--p2);border:1px solid var(--ln);font-size:12px;color:var(--dim);font-weight:600}
.ed-chip:hover{color:#fff;border-color:var(--ln2)}.ed-chip .pl{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;background:var(--p3);border:0}
.ed-chip .pl:hover{background:var(--lime);color:var(--ink)}.ed-chip.on{border-color:rgba(255,46,84,.6);color:#FFD6DF}
.ed-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:13px;background:var(--p2);border:1px solid var(--ln);box-shadow:inset 0 1px 0 rgba(255,255,255,.1);margin-bottom:8px}
.ed-row b{font-size:12.5px;font-weight:600}.ed-row span{display:block;color:var(--dim);font-size:11.5px;font-weight:400}
.ed-tg{width:36px;height:21px;border-radius:999px;background:var(--p3);position:relative;flex:none;border:0;padding:0}
.ed-tg::after{content:"";position:absolute;left:2px;top:2px;width:17px;height:17px;border-radius:50%;background:var(--dim);transition:transform .18s}
.ed-tg.on{background:var(--lime)}.ed-tg.on::after{transform:translateX(15px);background:var(--ink)}
.ed-f{display:block;margin-bottom:12px}.ed-f>span{display:flex;justify-content:space-between;font-size:12px;color:var(--dim);margin-bottom:6px}.ed-f>span b{color:#fff;font-weight:600}
.ed-in{width:100%;height:36px;border-radius:10px;border:1px solid var(--ln2);background:var(--p2);color:#fff;padding:0 11px;outline:none}
.ed-in:focus{border-color:var(--lime)}textarea.ed-in{height:auto;padding:9px 11px;resize:vertical;min-height:64px}
.ed-range{width:100%;-webkit-appearance:none;appearance:none;height:6px;border-radius:9px;margin:10px 0;outline:none;cursor:pointer;background:linear-gradient(90deg,var(--lime) var(--p,50%),var(--p3) var(--p,50%))}
.ed-range::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:#fff;border:4px solid var(--lime)}
.ed-range::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:#fff;border:4px solid var(--lime)}
.ed-range:hover::-webkit-slider-thumb{background:#FFE4EA}
.ed-seg{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:4px;padding:3px;border-radius:10px;background:var(--p2);border:1px solid var(--ln)}
.ed-seg button{border:0;background:transparent;border-radius:8px;padding:6px 4px;font-size:11.5px;font-weight:600;color:var(--dim)}
.ed-seg button.on{background:var(--p3);color:#fff;box-shadow:inset 0 0 0 1px var(--ln2)}
.ed-looks{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.ed-looks button{height:54px;border-radius:9px;background:#000 center/cover;position:relative;border:1px solid var(--ln);padding:0;overflow:hidden}
.ed-looks button span{position:absolute;bottom:3px;left:0;right:0;text-align:center;font-size:9.5px;font-weight:700;text-shadow:0 1px 3px #000}
.ed-looks button.on{outline:2px solid var(--lime);outline-offset:1px}
.ed-txfx{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.ed-txfx button{padding:12px 0;border-radius:10px;background:var(--p2);border:1px solid var(--ln);font-weight:900;font-size:14px}
.ed-txfx button.on{border-color:var(--lime);box-shadow:inset 0 0 0 1px var(--lime)}
.ed-styles{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
.ed-styles button{border-radius:10px;overflow:hidden;border:1px solid var(--ln);background:#000;padding:0;position:relative;aspect-ratio:3/4}
.ed-styles img{width:100%;height:100%;object-fit:cover;display:block}.ed-styles span{position:absolute;left:0;right:0;bottom:0;padding:14px 4px 4px;font-size:10.5px;font-weight:700;background:linear-gradient(transparent,rgba(0,0,0,.85))}
.ed-styles button.on{outline:2px solid var(--lime);outline-offset:1px}
.ed-layouts{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
.ed-layouts button{border-radius:10px;border:1px solid var(--ln);background:var(--p2);padding:8px 4px 7px;font-size:10.5px;font-weight:600;color:var(--dim);display:flex;flex-direction:column;align-items:center;gap:6px}
.ed-layouts button.on{border-color:var(--lime);color:#fff;box-shadow:inset 0 0 0 1px var(--lime)}
.ed-lay{width:30px;height:52px;border-radius:5px;background:#3a2d26;position:relative;overflow:hidden;border:1px solid var(--ln2)}
.ed-lay i{position:absolute;left:0;right:0;background:#8d7666}
.ed-trans{font-size:13.5px;line-height:2;color:#E9E1DB;max-height:none;user-select:none}
.ed-trans span{padding:2px 2px;border-radius:5px;cursor:pointer}.ed-trans span:hover{background:var(--p3)}
.ed-trans span.now{background:rgba(198,244,50,.18);color:var(--lime)}.ed-trans span.cut{text-decoration:line-through;color:#7d6f66;background:rgba(255,80,80,.08)}
.ed-trans span.sel{background:rgba(127,211,255,.25);color:#fff}.ed-trans span.out{opacity:.35}
.ed-kbd{font:10px "Geist Mono",ui-monospace,monospace;padding:1px 5px;border-radius:5px;background:var(--p3);color:var(--dim);border:1px solid var(--ln)}
.ed-keys{display:grid;grid-template-columns:auto 1fr;gap:7px 10px;font-size:12px;color:var(--dim);align-items:center}.ed-keys>span:nth-child(odd){display:flex;gap:3px;justify-self:start}
.ed-stage{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;min-height:0;min-width:0;
  background:radial-gradient(58% 60% at 50% 45%,rgba(12,5,11,.06),rgba(6,3,8,.30));padding:14px 10px 10px}
.ed-phone{position:relative;flex:1;min-height:0;aspect-ratio:9/16;max-width:100%;border-radius:20px;overflow:hidden;background:#000;border:1px solid var(--ln2);box-shadow:0 20px 56px rgba(0,0,0,.65)}
.ed-phone canvas{width:100%;height:100%;display:block}
.ed-safe{position:absolute;inset:9% 6% 18% 6%;border:1px dashed rgba(255,255,255,.3);border-radius:8px;pointer-events:none}
.ed-safe::after{content:"Safe zone: Shorts and Reels buttons sit outside it";position:absolute;bottom:-17px;left:0;right:0;text-align:center;font-size:9.5px;color:rgba(255,255,255,.6)}
.ed-hookbox{position:absolute;left:6%;right:6%;border:2px solid transparent;border-radius:10px;cursor:grab;touch-action:none}
.ed-hookbox:hover,.ed-hookbox.on{border-color:var(--lime);background:rgba(255,46,84,.08)}.ed-hookbox.on::before{content:"Drag to move";position:absolute;right:6px;top:-22px;font-size:10px;font-weight:700;background:var(--lime);color:#fff;padding:2px 7px;border-radius:999px}
.ed-capbox{position:absolute;left:4%;right:4%;border:2px dashed transparent;border-radius:10px;cursor:grab;touch-action:none}
.ed-capbox:hover{border-color:rgba(127,211,255,.7)}
.ed-loading{position:absolute;inset:0;display:grid;place-items:center;text-align:center;color:var(--dim);font-size:12.5px;padding:20px;background:#000}
.ed-loading b{display:block;color:#fff;font-size:14px;margin-bottom:6px}
.ed-spin{width:28px;height:28px;border-radius:50%;border:3px solid var(--p3);border-top-color:var(--lime);animation:edsp .8s linear infinite;margin:0 auto 12px}
@keyframes edsp{to{transform:rotate(360deg)}}
.ed-ctrl{display:flex;align-items:center;gap:12px;color:var(--dim);font-size:12px}
.ed-play{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,#fff,#e2e8f0);color:#09050b;display:grid;place-items:center;border:0;box-shadow:0 4px 16px rgba(0,0,0,.4)}
.ed-play svg{width:16px;height:16px}.ed-tc{font-family:"Geist Mono",ui-monospace,monospace;color:#fff;min-width:110px;text-align:center}
.ed-badge{position:absolute;top:12px;left:12px;display:flex;gap:6px;flex-wrap:wrap}.ed-badge span{font-size:11px;padding:4px 9px;border-radius:999px;background:rgba(18,10,16,.75);backdrop-filter:blur(12px);border:1px solid var(--ln);color:var(--dim)}
.ed-badge span b{color:#FF7A95}
.ed-badge2{position:absolute;top:12px;right:12px}
.ed-sel{display:flex;align-items:center;gap:10px;padding:10px;border-radius:12px;background:rgba(255,46,84,.12);border:1px solid rgba(255,46,84,.36);margin-bottom:14px}
.ed-sel i{width:32px;height:32px;border-radius:9px;background:var(--lime);display:grid;place-items:center;color:#fff;font-style:normal;font-weight:900;font-size:15px;flex:none}
.ed-sel b{display:block}.ed-sel span{color:var(--dim);font-size:11.5px}
.ed-stat{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}.ed-stat div{padding:10px;border-radius:11px;background:var(--p2);border:1px solid var(--ln)}
.ed-stat b{display:block;font-size:17px;letter-spacing:-.02em}.ed-stat span{font-size:11px;color:var(--dim)}
.ed-score{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:700;padding:4px 10px;border-radius:999px;background:rgba(255,46,84,.2);color:#FF8BA3;border:1px solid rgba(255,46,84,.36)}
/* timeline */
.ed-tl{background:var(--p1);backdrop-filter:blur(28px) saturate(185%);-webkit-backdrop-filter:blur(28px) saturate(185%);border-top:1px solid var(--ln);display:grid;grid-template-rows:38px minmax(0,1fr);min-height:0;position:relative}
.ed-tlresize{position:absolute;left:0;right:0;top:-4px;height:8px;cursor:ns-resize;z-index:9}
.ed-tlbar{display:flex;align-items:center;gap:6px;padding:0 10px;border-bottom:1px solid var(--ln);color:var(--dim);min-width:0;overflow:hidden}
.ed-tool{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 9px;border-radius:8px;border:1px solid var(--ln);background:transparent;font-size:11.5px;font-weight:600;color:var(--dim);white-space:nowrap}
.ed-tool:hover:not(:disabled){color:#fff;background:var(--p2)}.ed-tool:disabled{opacity:.4;cursor:default}.ed-tool svg{width:14px;height:14px}
.ed-tracks{display:grid;grid-template-columns:96px minmax(0,1fr);min-height:0;overflow:hidden}
.ed-lbls{border-right:1px solid var(--ln);display:flex;flex-direction:column}
.ed-lbls div{display:flex;align-items:center;gap:7px;padding:0 10px;color:var(--dim);font-size:11px;font-weight:600;border-bottom:1px solid var(--ln);white-space:nowrap}
.ed-lane{position:relative;overflow-x:auto;overflow-y:hidden;scrollbar-color:#3a2d26 transparent}
.ed-inner{position:relative;height:100%}
.ed-ruler{height:20px;border-bottom:1px solid var(--ln);position:relative;font:10px "Geist Mono",ui-monospace,monospace;color:var(--faint);cursor:pointer}
.ed-ruler span{position:absolute;top:4px;white-space:nowrap}.ed-ruler i{position:absolute;bottom:0;width:1px;height:5px;background:var(--faint)}
.ed-hype{height:14px;position:relative;border-bottom:1px solid var(--ln)}
.ed-hype canvas{position:absolute;inset:0;width:100%;height:100%}
.ed-ln{position:relative;border-bottom:1px solid var(--ln)}
.ed-vid{position:absolute;top:4px;bottom:4px;border-radius:7px;background-repeat:no-repeat;background-color:#000;opacity:.4}
.ed-clipr{position:absolute;top:2px;bottom:2px;border:2px solid var(--lime);border-radius:9px;pointer-events:none}
.ed-vidin{position:absolute;top:4px;bottom:4px;border-radius:7px;background-repeat:no-repeat;background-color:#000}
.ed-cut{position:absolute;top:2px;bottom:2px;background:repeating-linear-gradient(-45deg,rgba(14,11,9,.92) 0 6px,rgba(255,90,90,.35) 6px 8px);border-radius:6px;cursor:pointer;border:1px solid rgba(255,90,90,.5)}
.ed-cut.sel{outline:2px solid #fff}
.ed-cut em{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-style:normal;font-size:10px;font-weight:800;color:#FFB3B3;background:rgba(14,11,9,.85);padding:1px 6px;border-radius:6px;white-space:nowrap;pointer-events:none}
.ed-cut::before,.ed-cut::after{content:"";position:absolute;top:0;bottom:0;width:7px;cursor:ew-resize}.ed-cut::before{left:-2px}.ed-cut::after{right:-2px}
.ed-cut:hover{border-color:#FF8A8A}
.ed-seg2{position:absolute;top:4px;bottom:4px;border-radius:7px;cursor:pointer}.ed-seg2:hover{background:rgba(255,255,255,.06)}
.ed-seg2.sel{box-shadow:inset 0 0 0 2px #7FD3FF;background:rgba(127,211,255,.12)}
.ed-rng{position:absolute;top:0;bottom:0;background:rgba(127,211,255,.18);border-left:2px solid #7FD3FF;border-right:2px solid #7FD3FF;pointer-events:none;z-index:3}
.ed-rng span{position:absolute;left:50%;top:-1px;transform:translateX(-50%);font-size:10px;font-weight:800;background:#7FD3FF;color:#08202c;padding:0 6px;border-radius:0 0 6px 6px;white-space:nowrap}
.ed-vl,.ed-cl{cursor:text}
.ed-split{position:absolute;top:0;bottom:0;width:2px;background:#fff;opacity:.7;pointer-events:none}
.ed-handle{position:absolute;top:2px;bottom:2px;width:12px;background:var(--lime);border-radius:4px;cursor:ew-resize;z-index:3;display:grid;place-items:center;touch-action:none}
.ed-handle::after{content:"";width:2px;height:16px;border-radius:2px;background:var(--ink);opacity:.6}
.ed-tr{position:absolute;top:50%;width:20px;height:20px;margin:-10px 0 0 -10px;transform:rotate(45deg);background:#fff;border-radius:4px;z-index:4;cursor:grab;
  box-shadow:0 0 0 3px rgba(255,46,84,.48);touch-action:none}
.ed-tr.sel{background:var(--lime);box-shadow:0 0 0 3px #fff}
.ed-word{position:absolute;top:6px;bottom:6px;border-radius:5px;background:rgba(255,255,255,.07);border:1px solid var(--ln);font-size:10.5px;padding:0 5px;
  color:#E9E1DB;white-space:nowrap;overflow:hidden;display:flex;align-items:center;cursor:pointer}
.ed-word.now{background:rgba(255,46,84,.22);border-color:rgba(255,46,84,.55);color:#FFD6DF}
.ed-word.cut{text-decoration:line-through;opacity:.35}
.ed-txt{position:absolute;top:5px;bottom:5px;border-radius:7px;background:linear-gradient(90deg,#7FD3FF,#9b8cff);color:#0b1020;font-size:11px;font-weight:800;
  padding:0 9px;display:flex;align-items:center;white-space:nowrap;overflow:hidden;cursor:pointer}
.ed-txt.sel{outline:2px solid #fff}
.ed-sfx{position:absolute;top:5px;bottom:5px;border-radius:7px;background:#FFB86B;color:#2a1600;font-size:10.5px;font-weight:800;padding:0 8px;
  display:flex;align-items:center;gap:4px;white-space:nowrap;cursor:grab;touch-action:none}
.ed-sfx.sel{outline:2px solid #fff}
.ed-pun{position:absolute;top:5px;bottom:5px;border-radius:7px;background:rgba(255,46,84,.18);border:1px dashed rgba(255,105,135,.75);color:#FFD6DF;font-size:10.5px;
  font-weight:800;padding:0 8px;display:flex;align-items:center;white-space:nowrap;overflow:hidden;cursor:grab;touch-action:none}
.ed-pun.sel{outline:2px solid #fff}
.ed-med{position:absolute;top:5px;bottom:5px;border-radius:7px;background:linear-gradient(90deg,#FF7AD9,#B57CFF);color:#1a0820;font-size:10.5px;font-weight:800;
  padding:0 8px 0 4px;display:flex;align-items:center;gap:5px;white-space:nowrap;overflow:hidden;cursor:grab;touch-action:none}
.ed-med img{height:22px;width:22px;object-fit:cover;border-radius:4px;flex:none}.ed-med.sel{outline:2px solid #fff}
.ed-mbox{position:absolute;border:2px solid #FF7AD9;border-radius:6px;cursor:move;touch-action:none}
.ed-mbox i{position:absolute;right:-7px;bottom:-7px;width:14px;height:14px;border-radius:4px;background:#FF7AD9;cursor:nwse-resize}
.ed-lib{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
.ed-lib .it{position:relative;border-radius:10px;overflow:hidden;background:var(--p2);border:1px solid var(--ln);aspect-ratio:1;display:grid;place-items:center}
.ed-lib .it img{width:100%;height:100%;object-fit:cover}.ed-lib .it span{position:absolute;left:4px;bottom:4px;font-size:9.5px;font-weight:800;background:rgba(0,0,0,.7);padding:1px 5px;border-radius:5px}
.ed-lib .it .acts{position:absolute;inset:auto 4px 4px auto;display:flex;gap:3px}
.ed-lib .it button{width:24px;height:24px;border-radius:7px;border:0;background:rgba(0,0,0,.72);display:grid;place-items:center;font-weight:800}
.ed-lib .it button:hover{background:var(--lime);color:var(--ink)}
.ed-drop{border:1.5px dashed var(--ln2);border-radius:14px;padding:16px;text-align:center;color:var(--dim);font-size:12px;margin-bottom:16px;cursor:pointer}
.ed-drop:hover,.ed-drop.over{border-color:var(--lime);color:#fff;background:rgba(255,46,84,.08)}.ed-drop b{display:block;color:#fff;font-size:13px;margin-bottom:3px}
.ed-font{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:11px;background:var(--p2);border:1px solid var(--ln);margin-bottom:6px}
.ed-font b{font-size:20px;font-weight:400;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ed-fonts{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.ed-fonts button{padding:7px 10px;border-radius:9px;background:var(--p2);border:1px solid var(--ln);font-size:15px}
.ed-fonts button.on{border-color:var(--lime);box-shadow:inset 0 0 0 1px var(--lime)}
.ed-wave{position:absolute;inset:3px 0}
.ed-ph{position:absolute;top:0;bottom:0;width:2px;background:#fff;z-index:8;pointer-events:none}
.ed-ph::before{content:"";position:absolute;top:-1px;left:-6px;border:7px solid transparent;border-top:9px solid #fff}
.ed-hot{position:absolute;top:-1px;font-size:10px;transform:translateX(-50%);pointer-events:none}
.ed-exp{position:absolute;inset:0;z-index:20;background:rgba(8,4,10,.78);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);display:grid;place-items:center}
.ed-exp .card{width:min(420px,92vw);background:linear-gradient(155deg,rgba(255,255,255,.10),rgba(20,10,16,.84));border:1px solid var(--ln2);box-shadow:inset 0 1px 0 rgba(255,255,255,.22),0 24px 64px rgba(0,0,0,.6);border-radius:22px;padding:26px;text-align:center}
.ed-exp h3{margin:0 0 6px;font-size:18px}.ed-exp p{color:var(--dim);margin:0 0 16px}
.ed-bar{height:6px;border-radius:9px;background:var(--p3);overflow:hidden;margin:14px 0}.ed-bar i{display:block;height:100%;width:40%;background:var(--lime);border-radius:9px;animation:edbar 1.4s ease-in-out infinite}
.ed-pct{font-size:44px;font-weight:800;letter-spacing:-.04em;color:#FF7A95;line-height:1;margin:2px 0 4px;font-variant-numeric:tabular-nums}
.ed-pct small{font-size:20px;margin-left:2px}
.ed-meter{position:relative;height:14px;border-radius:999px;background:var(--p3);overflow:hidden;margin:16px 0 8px}
.ed-meter i{position:absolute;left:0;top:0;bottom:0;width:0;border-radius:999px;background:linear-gradient(90deg,#D6123A,#FF5C7C);transition:width .5s cubic-bezier(.3,.7,.2,1)}
.ed-meter i::after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(-45deg,rgba(255,255,255,.18) 0 10px,transparent 10px 20px);background-size:28px 28px;animation:edstripe .9s linear infinite}
.ed-meter.wait i{width:100%!important;background:var(--p2)}.ed-meter.wait i::after{background:repeating-linear-gradient(-45deg,rgba(255,255,255,.07) 0 10px,transparent 10px 20px)}
@keyframes edstripe{to{background-position:28px 0}}
.ed-steps{display:flex;justify-content:space-between;gap:6px;margin:0 0 16px;font-size:11.5px;color:var(--faint)}
.ed-steps span{display:flex;align-items:center;gap:5px}.ed-steps span::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--p3)}
.ed-steps span.on{color:var(--t)}.ed-steps span.on::before{background:var(--lime)}.ed-steps span.done{color:var(--dim)}.ed-steps span.done::before{background:var(--lime);opacity:.5}
.ed-eta{font-size:12px;color:var(--dim);display:flex;justify-content:space-between;margin-bottom:6px}
@keyframes edbar{0%{transform:translateX(-100%)}100%{transform:translateX(260%)}}
.ed-exp .row{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
@media (max-width:1280px){.ed-mid{grid-template-columns:64px 268px minmax(0,1fr) 250px}}
@media (max-width:1080px){.ed-mid{grid-template-columns:64px 260px minmax(0,1fr)}.ed-insp{display:none}}
`;

  let E = null;          // the open editor

  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const r3 = v => Math.round(v * 1000) / 1000;
  const tc = (s, ms = true) => { s = Math.max(0, s); const m = Math.floor(s / 60), x = s - m * 60; return String(m).padStart(2, "0") + ":" + (ms ? x.toFixed(1).padStart(4, "0") : String(Math.floor(x)).padStart(2, "0")); };
  const deep = o => JSON.parse(JSON.stringify(o));
  const eic = n => (typeof ic === "function" && I[n]) ? ic(n) : "";

  // ---------------------------------------------------------------- the edit's own maths (the same as studio/edit.py)
  function cutsMerged(cuts, start, end) {
    const c = (cuts || []).map(([a, b]) => [Math.max(start, Math.min(a, b)), Math.min(end, Math.max(a, b))]).filter(([a, b]) => b - a >= 0.05).sort((x, y) => x[0] - y[0]);
    const out = [];
    for (const [a, b] of c) { if (out.length && a <= out[out.length - 1][1] + 0.02) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b); else out.push([a, b]); }
    return out;
  }
  function keepOf(doc) {          // kept parts, in source time
    const cuts = cutsMerged(doc.edit.cuts, doc.start, doc.end), keep = []; let at = doc.start;
    for (const [a, b] of cuts) { if (a - at > 0.05) keep.push([at, a]); at = Math.max(at, b); }
    if (doc.end - at > 0.05) keep.push([at, doc.end]);
    return keep;
  }
  function outLen(doc) { return keepOf(doc).reduce((t, [a, b]) => t + b - a, 0); }
  function toOut(doc, t) {        // source time -> finished clip time (null when cut out)
    let out = 0;
    for (const [a, b] of keepOf(doc)) { if (t < a - 0.05) return out > 0 ? out : null; if (t <= b + 0.05) return out + clamp(t - a, 0, b - a); out += b - a; }
    return null;
  }
  function fromOut(doc, o) {      // finished clip time -> source time
    let acc = 0; const k = keepOf(doc);
    for (const [a, b] of k) { if (o <= acc + (b - a)) return a + (o - acc); acc += b - a; }
    return k.length ? k[k.length - 1][1] : doc.start;
  }
  function inCut(doc, t) { return cutsMerged(doc.edit.cuts, doc.start, doc.end).find(([a, b]) => t >= a && t < b) || null; }
  function joins(doc) {           // where the cuts meet the kept parts, plus splits: places for transitions
    const j = cutsMerged(doc.edit.cuts, doc.start, doc.end).map(([, b]) => b).filter(t => t > doc.start + 0.1 && t < doc.end - 0.1);
    for (const s of doc.splits || []) if (s > doc.start + 0.1 && s < doc.end - 0.1 && !inCut(doc, s)) j.push(s);
    return [...new Set(j.map(r3))].sort((a, b) => a - b);
  }
  function bump(t, at, h) { return Math.max(0, 1 - Math.abs(t - at) / h); }

  // ---------------------------------------------------------------- opening
  function docOf(clip) {          // a clip's edit, as the editor works on it
    const ed = clip.edit || {};
    return {start: clip.start, end: clip.end, hook: clip.hook || "", title: clip.title || "", description: clip.description || "",
      style: clip.style || "bounce", layout: clip.layout || "blur", captions: clip.captions !== false, tighten: !!clip.tighten, zoom: !!clip.zoom,
      watermark: clip.watermark || "", splits: [], split: clip.split || "balanced",
      edit: {cuts: deep(ed.cuts || []), fx: deep(ed.fx || []), sfx: deep(ed.sfx || []), punch: deep(ed.punch || []), look: ed.look || "natural",
             hook_fx: ed.hook_fx || "box", hook_y: ed.hook_y || null, cap_y: ed.cap_y || null, hook_dur: ed.hook_dur || 0,
             media: deep(ed.media || []), hook_font: ed.hook_font || "", cap_font: ed.cap_font || "", voice: ed.voice == null ? 1 : ed.voice}};
  }
  async function open(jid, cid) {
    const job = jobOf(jid), clip = job && job.clips.find(c => c.id === cid);
    if (!clip) return;
    if (E) close(true);
    document.querySelector('#tour [data-tour="close"]')?.click();      // the first-run tour never sits over the editor
    if (!document.getElementById("ed-css")) { const st = document.createElement("style"); st.id = "ed-css"; st.textContent = css; document.head.append(st); }
    const doc = docOf(clip);
    E = {jid, cid, doc, saved: JSON.stringify(doc), hist: [JSON.stringify(doc)], hi: 0, tool: "edit", sel: null, pps: 40, data: null,
         playing: false, t: clip.start, tsel: [], lib: [], libEl: {}, draftKey: `ck-edit:${jid}:${cid}:${clip.version || 0}`, safe: false, exporting: false};
    try {     // an edit not exported yet, kept from last time
      const dr = localStorage.getItem(E.draftKey);
      if (dr) { const d = JSON.parse(dr); if (d && d.edit) { d.edit = Object.assign({media: [], hook_font: "", cap_font: ""}, d.edit); E.doc = Object.assign(doc, d); E.hist = [JSON.stringify(E.doc)]; E.restored = true; } }
    } catch {}
    const root = document.createElement("div"); root.id = "ed"; root.tabIndex = -1;
    document.body.append(root); E.root = root;
    try { document.body.style.overflow = "hidden"; } catch {}
    shell();
    root.focus();
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onResize);
    try {
      E.data = await api(`/api/jobs/${encodeURIComponent(jid)}/clips/${encodeURIComponent(cid)}/editor`);
    } catch (e) {
      if (!E) return;
      E.root.querySelector(".ed-loading").innerHTML = `<div><b>This clip can't be opened in the editor</b>${esc(e.message)}<br><br><button class="ed-btn" data-e="close">Close</button></div>`;
      return;
    }
    if (!E) return;
    await loadFonts();
    loadLibrary();
    startMedia();
    if (E.restored) toast("Your unsaved edit of this clip was brought back. Export to make it.", "ok");
  }

  function loadFonts() {
    const files = new Set([E.data.hook.file, ...Object.values(E.data.styles).map(s => s.file).filter(Boolean)]);
    const fam = {};
    Object.values(E.data.styles).forEach(s => { if (s.file) fam[s.file] = s.font; });
    fam[E.data.hook.file] = E.data.hook.font;
    return Promise.all([...files].map(f => {
      if ([...document.fonts].some(x => x.family === fam[f] || x.family === `"${fam[f]}"`)) return null;
      const ff = new FontFace(fam[f], `url(/api/edit/font/${encodeURIComponent(f)})`);
      return ff.load().then(x => document.fonts.add(x)).catch(() => null);
    }));
  }

  function close(force) {
    if (!E) return;
    if (!force && E.doc && JSON.stringify(E.doc) !== E.saved && !E.exported) {
      try { localStorage.setItem(E.draftKey, JSON.stringify(E.doc)); } catch {}
      toast("Your edit is kept. Open the clip again to carry on, then Export.", "ok");
    }
    try { E.v && E.v.pause(); } catch {}
    E.sfxAudio && E.sfxAudio.forEach(a => a.pause());
    Object.values(E.libEl || {}).forEach(el => { try { el.pause && el.pause(); } catch {} });
    cancelAnimationFrame(E.raf);
    clearInterval(E.poll);
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", onResize);
    E.root.remove();
    document.body.style.overflow = "";
    E = null;
    try { if (typeof loadLib === "function") loadLib(); } catch {}     // files added in the editor show in the app's Library too
  }

  // ---------------------------------------------------------------- history
  function commit(label) {
    const s = JSON.stringify(E.doc);
    if (s === E.hist[E.hi]) return;
    E.hist = E.hist.slice(0, E.hi + 1); E.hist.push(s); if (E.hist.length > 120) E.hist.shift(); E.hi = E.hist.length - 1;
    try { localStorage.setItem(E.draftKey, s); } catch {}
    E.lastLabel = label || "";
    refresh();
  }
  function undoTo() { E.doc = JSON.parse(E.hist[E.hi]); refresh(); }     // back to the last saved step, without a new one
  function undo() { if (E.hi > 0) { E.hi--; E.doc = JSON.parse(E.hist[E.hi]); E.sel = null; refresh(); } }
  function redo() { if (E.hi < E.hist.length - 1) { E.hi++; E.doc = JSON.parse(E.hist[E.hi]); E.sel = null; refresh(); } }

  // ---------------------------------------------------------------- the frame of the editor
  const RAIL = [["edit", "scissors", "Edit"], ["frame", "video", "Frame"], ["captions", "chat", "Captions"], ["text", "film", "Text"],
                ["effects", "sparkle", "Effects"], ["audio", "radio", "Audio"], ["library", "folder", "Library"], ["post", "send", "Post"]];
  function shell() {
    const job = jobOf(E.jid), clip = job.clips.find(c => c.id === E.cid);
    const n = job.clips.findIndex(c => c.id === E.cid) + 1;
    E.root.innerHTML = `
      <div class="ed-top">
        <button class="ed-ib" data-e="close" title="Back to the clips (Esc)" aria-label="Back">${eic("arrowr").replace("<svg", '<svg style="transform:rotate(180deg)"')}</button>
        <img class="ed-brand" src="/brand/avatar.png?v=tatya1" alt="PapiClips">
        <div class="ed-ttl"><b>${esc(clip.title || clip.hook || "Clip")}</b><span>Clip ${n} of ${job.clips.length} · ${esc(job.title || "")}</span></div>
        <span class="ed-state" id="edState"><i></i><span>Saved</span></span>
        <span class="ed-sp"></span>
        <button class="ed-ib" data-e="undo" id="edUndo" title="Undo (Ctrl+Z)">↶</button><button class="ed-ib" data-e="redo" id="edRedo" title="Redo (Ctrl+Shift+Z)">↷</button>
        <button class="ed-btn ai" data-e="hype" title="One click: cuts filler words, punches in on the hype moments, adds transitions, sounds and a look">${eic("sparkle")}Hype Edit</button>
        <button class="ed-btn p" data-e="export" id="edExport">${eic("down")}Export clip</button>
      </div>
      <div class="ed-mid">
        <nav class="ed-rail">${RAIL.map(([k, i, l]) => `<button data-tool="${k}" class="${E.tool === k ? "on" : ""}">${eic(i)}${l}</button>`).join("")}
          <button class="ai" data-tool="ai">${eic("bolt")}AI</button></nav>
        <aside class="ed-panel" id="edPanel"></aside>
        <section class="ed-stage">
          <div class="ed-badge" id="edBadge"><span>Live preview · <b>no waiting</b></span></div>
          <div class="ed-phone" id="edPhone"><canvas id="edCanvas" width="540" height="960"></canvas>
            <div class="ed-hookbox" id="edHookBox" title="Drag to move the big text"></div><div class="ed-capbox" id="edCapBox" title="Drag to move the captions"></div>
            <div class="ed-loading"><div><div class="ed-spin"></div><b>Opening the editor</b>Getting this clip ready (a few seconds the first time)</div></div></div>
          <div class="ed-ctrl"><button class="ed-ib" data-e="prev" title="Back 1 s (Shift+←)">⏮</button><button class="ed-play" data-e="play" id="edPlay" title="Play / pause (Space)">${eic("play_f")}</button>
            <button class="ed-ib" data-e="next" title="Forward 1 s (Shift+→)">⏭</button><span class="ed-tc" id="edTc">00:00.0 / 00:00.0</span>
            <button class="ed-ib" data-e="mute" id="edMute" title="Sound on / off">🔊</button><button class="ed-ib" data-e="safe" id="edSafe" title="Show the safe zone">▣</button></div>
        </section>
        <aside class="ed-insp" id="edInsp"></aside>
      </div>
      <div class="ed-tl" id="edTl"><div class="ed-tlresize" id="edTlResize"></div>
        <div class="ed-tlbar">
          <button class="ed-tool" data-e="split" title="Split at the playhead (S)">${eic("scissors")}Split <span class="ed-kbd">S</span></button>
          <button class="ed-tool" data-e="del" id="edDel" title="Delete what's selected (Del)">${eic("trash")}Delete <span class="ed-kbd">Del</span></button>
          <button class="ed-tool" data-e="in" title="Start the clip here (I)">[ Start here <span class="ed-kbd">I</span></button>
          <button class="ed-tool" data-e="out" title="End the clip here (O)">End here ] <span class="ed-kbd">O</span></button>
          <button class="ed-tool" data-e="addtr" title="Add a transition at the playhead (T)">◆ Transition <span class="ed-kbd">T</span></button>
          <button class="ed-tool" data-e="addpunch" title="Punch in at the playhead (P)">⤢ Punch-in <span class="ed-kbd">P</span></button>
          <span class="ed-sp"></span><span id="edLen" style="white-space:nowrap"></span>
          <button class="ed-tool" data-e="zout" title="Zoom out (-)">−</button><button class="ed-tool" data-e="zfit" title="Fit the clip">Fit</button><button class="ed-tool" data-e="zin" title="Zoom in (+)">+</button>
        </div>
        <div class="ed-tracks"><div class="ed-lbls" id="edLbls"></div><div class="ed-lane" id="edLane"><div class="ed-inner" id="edInner"></div></div></div>
      </div>`;
    E.root.addEventListener("click", onClick);
    // the app's own page-wide click handlers stay out of the editor (only its "copy" and "campaign" buttons use them)
    E.root.addEventListener("click", e => { if (!e.target.closest("[data-act]")) e.stopPropagation(); });
    E.root.addEventListener("input", onInput);
    E.root.addEventListener("change", onChange);
    E.root.addEventListener("pointerdown", onDown);
    // files dropped anywhere on the editor go into the library
    E.root.addEventListener("dragover", e => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); E.root.querySelector("#edDrop")?.classList.add("over"); } });
    E.root.addEventListener("drop", e => { if (!e.dataTransfer || !e.dataTransfer.files.length) return; e.preventDefault(); E.tool = "library"; drawPanel(); upload([...e.dataTransfer.files]); });
    E.cv = E.root.querySelector("#edCanvas"); E.ctx = E.cv.getContext("2d");
    E.lane = E.root.querySelector("#edLane"); E.inner = E.root.querySelector("#edInner");
    E.lane.addEventListener("wheel", onWheel, {passive: false});
    // sliders show how far along they are (the lime part), whenever a panel is drawn and as they move
    const fill = r => r.style.setProperty("--p", `${(Number(r.value) - Number(r.min || 0)) / ((Number(r.max) || 1) - Number(r.min || 0)) * 100}%`);
    new MutationObserver(() => E && E.root.querySelectorAll(".ed-range").forEach(fill)).observe(E.root, {childList: true, subtree: true});
    E.root.addEventListener("input", e => { if (e.target.classList && e.target.classList.contains("ed-range")) fill(e.target); });
    drawPanel(); drawInsp(); drawState();
  }

  function startMedia() {
    const v = document.createElement("video");
    v.src = E.data.preview; v.preload = "auto"; v.playsInline = true; v.crossOrigin = "anonymous";
    v.muted = false; E.v = v; voiceVol();
    v.addEventListener("loadeddata", () => {
      if (!E) return;
      E.root.querySelector(".ed-loading")?.remove();
      seek(E.doc.start + 0.01);
      fitZoom(); drawTimeline(); drawPanel(); drawInsp(); drawFrame();
    }, {once: true});
    v.addEventListener("seeked", () => E && drawFrame());
    v.addEventListener("error", () => { const l = E && E.root.querySelector(".ed-loading"); if (l) l.innerHTML = "<div><b>The preview couldn't be played</b>Close the editor and open the clip again.</div>"; });
    E.strip = new Image(); E.strip.src = E.data.strip;
    E.strip.onload = () => { if (!E) return; E.tw = E.strip.naturalWidth / Math.max(1, E.data.thumbs); drawTimeline(); drawPanel(); };
    E.sfxAudio = [];
    loop();
  }

  // ---------------------------------------------------------------- playing
  function now() { return E.v ? E.data.p0 + E.v.currentTime : E.t; }
  function seek(t) {
    if (!E.v) return;
    t = clamp(t, E.data.p0, E.data.p1 - 0.05);
    E.t = t;
    try { E.v.currentTime = t - E.data.p0; } catch {}
    tickUi();
  }
  function play() {
    if (!E.v) return;
    if (E.playing) { E.v.pause(); E.playing = false; drawPlay(); return; }
    let t = now();
    if (t < E.doc.start || t >= E.doc.end - 0.05) seek(E.doc.start);
    E.playing = true; E.lastT = now(); drawPlay();
    E.v.play().catch(() => { E.playing = false; drawPlay(); });
  }
  function drawPlay() { const b = E.root.querySelector("#edPlay"); if (b) b.innerHTML = eic(E.playing ? "pause" : "play_f"); }
  function loop() {
    if (!E) return;
    if (E.playing && E.v) {
      let t = now();
      const c = inCut(E.doc, t);
      if (c) { seek(c[1] + 0.001); t = c[1]; }           // a part that was cut out is skipped, as in the finished clip
      if (t >= E.doc.end - 0.02) { E.v.pause(); E.playing = false; seek(E.doc.start); drawPlay(); }
      fireSounds(E.lastT || t, t);
      E.lastT = t;
      drawFrame(); tickUi();
    }
    E.raf = requestAnimationFrame(loop);
  }
  function fireSounds(a, b) {       // sound effects and transition sounds, heard as the playhead passes them
    if (b < a || b - a > 0.5 || E.v.muted) return;
    const list = E.doc.edit.sfx.map(s => [s.t - ({whoosh: .45, swoosh: .18, riser: 1.6}[s.kind] || 0), s.kind, s.vol])
      .concat(E.doc.edit.fx.filter(f => f.sound !== false && (E.data.options.transition_sound[f.type])).map(f => [f.t - ({whoosh: .45, swoosh: .18}[E.data.options.transition_sound[f.type]] || 0), E.data.options.transition_sound[f.type], 0.8]));
    for (const [t, k, vol] of list) if (t > a && t <= b) hear(k, vol);
  }
  function sfxName(kind) { if (String(kind).startsWith("lib:")) { const it = libItem(kind.slice(4)); return it ? it.name : "Deleted sound"; } return (E.data.options.sounds || {})[kind] || kind; }
  function hear(kind, vol = 0.8) {
    const lib = String(kind).startsWith("lib:") ? libItem(kind.slice(4)) : null;
    if (String(kind).startsWith("lib:") && !lib) return;
    const a = new Audio(lib ? lib.url : `/api/edit/sound/${kind}.wav`); a.volume = clamp(vol * 0.8, 0, 1); a.play().catch(() => {});
    E.sfxAudio.push(a); if (E.sfxAudio.length > 12) E.sfxAudio.shift();
  }

  // ---------------------------------------------------------------- the live preview
  function srcScale() { return E.v && E.v.videoWidth ? E.v.videoWidth / (E.data.src_w || E.v.videoWidth) : 1; }
  function cover(ctx, v, sx, sy, sw, sh, dx, dy, dw, dh) {    // draw part of the video so it fills a box, cropped around its middle
    const r = Math.max(dw / sw, dh / sh), cw = dw / r, ch = dh / r;
    ctx.drawImage(v, sx + (sw - cw) / 2, sy + (sh - ch) / 2, cw, ch, dx, dy, dw, dh);
  }
  function drawFrame() {
    if (!E || !E.v || !E.v.videoWidth) return;
    const ctx = E.ctx, v = E.v, d = E.data, doc = E.doc, ed = doc.edit;
    const t = now(), o = toOut(doc, t), ot = o == null ? -1 : o;
    const vw = v.videoWidth, vh = v.videoHeight, k = srcScale();
    ctx.setTransform(0.5, 0, 0, 0.5, 0, 0);
    ctx.filter = "none"; ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
    // the push-ins: punch-ins, and the zoom / whoosh / spin / shake transitions
    let z = 1, ang = 0, sx = 0, sy = 0, blur = 0, glitch = 0, flash = 0, dark = 0, wipe = null;
    for (const p of ed.punch) { const at = toOut(doc, p.t); if (at == null) continue; z += (p.z - 1) * clamp((ot - (at - 0.12)) / 0.15, 0, 1) * clamp((at + p.d - ot) / 0.3, 0, 1); }
    for (const f of ed.fx) {
      const at = toOut(doc, f.t); if (at == null) continue;
      const h = Math.max(0.05, f.d / 2), b = bump(ot, at, h);
      if (!b && f.type !== "wipe") continue;
      if (f.type === "zoom") { z += 0.45 * b; if (Math.abs(ot - at) < h / 2) blur = Math.max(blur, 8); }
      else if (f.type === "whoosh") { z += 0.10 * b; blur = Math.max(blur, 14 * (b > 0 ? 1 : 0)); }
      else if (f.type === "spin") { z += 0.30 * b; const q = clamp((ot - (at - h)) / (2 * h), 0, 1); ang += 0.35 * Math.sin(Math.PI * q) * Math.sin(2 * Math.PI * q); }
      else if (f.type === "shake") { const s = bump(ot, at, h * 1.6); z += 0.06 * b; sx += 26 * Math.sin(ot * 73) * s; sy += 46 * Math.cos(ot * 61) * s; }
      else if (f.type === "blur") blur = Math.max(blur, 26 * (b > 0 ? 1 : 0));
      else if (f.type === "glitch") glitch = Math.max(glitch, b > 0 ? 1 : 0);
      else if (f.type === "flash") flash += 0.85 * b;
      else if (f.type === "fade") dark += b;
      else if (f.type === "wipe" && ot >= at - h && ot <= at + h) wipe = (ot - (at - h)) / (2 * h);
    }
    const look = (d.options.looks[ed.look] || {}).css || "none";
    ctx.save();
    ctx.translate(W / 2 + sx, H / 2 + sy); ctx.rotate(ang); ctx.scale(z, z); ctx.translate(-W / 2, -H / 2);
    const lookF = look === "none" ? "" : look;
    const f2 = (extra = "") => (lookF + " " + extra + (blur ? ` blur(${blur}px)` : "")).trim() || "none";
    const G = geom(), lay = G.lay, face = d.facecam && d.facecam.length === 4 ? d.facecam.map(x => x * k) : null, game = d.game ? d.game.map(x => x * k) : null;
    const g = game || [0, 0, vw, vh];
    if (lay === "split" && face) {
      ctx.filter = f2(); cover(ctx, v, face[0], face[1], face[2], face[3], 0, 0, W, G.top);
      cover(ctx, v, g[0], g[1], g[2], g[3], 0, G.top, W, H - G.top);
    } else if (lay === "gameplay" && face) {
      ctx.filter = f2(); cover(ctx, v, g[0], g[1], g[2], g[3], 0, 0, W, H);
      const pw = d.pip_w, ph = pw * face[3] / face[2];
      ctx.drawImage(v, face[0], face[1], face[2], face[3], W - pw - d.pip_margin, d.pip_margin, pw, ph);
    } else if (lay === "crop" || ((lay === "split" || lay === "gameplay") && !face)) {
      ctx.filter = f2(); cover(ctx, v, 0, 0, vw, vh, 0, 0, W, H);
    } else {                       // blurred fill behind the whole picture
      ctx.filter = f2("blur(28px) brightness(.86) saturate(1.15)"); cover(ctx, v, 0, 0, vw, vh, -60, -60, W + 120, H + 120);
      ctx.filter = f2(); const fh = G.fg; cover(ctx, v, 0, 0, vw, vh, 0, (H - fh) / 2, W, fh);
    }
    ctx.restore(); ctx.filter = "none";
    if (glitch) {               // an RGB split: the picture shifted, tinted, added on top
      ctx.save(); ctx.globalCompositeOperation = "lighter"; ctx.globalAlpha = 0.35;
      ctx.drawImage(E.cv, 0, 0, E.cv.width, E.cv.height, 18, 0, W, H); ctx.fillStyle = "rgba(255,0,80,.12)"; ctx.fillRect(0, 0, W, H);
      ctx.drawImage(E.cv, 0, 0, E.cv.width, E.cv.height, -18, 6, W, H); ctx.restore();
    }
    if (flash > 0) { ctx.fillStyle = `rgba(255,255,255,${clamp(flash, 0, 0.95)})`; ctx.fillRect(0, 0, W, H); }
    if (dark > 0) { ctx.fillStyle = `rgba(0,0,0,${clamp(dark, 0, 1)})`; ctx.fillRect(0, 0, W, H); }
    if (wipe != null) { ctx.fillStyle = "#C6F432"; ctx.fillRect(-W + wipe * 2 * W, 0, W, H); }
    drawMedia(ctx, ot);
    drawCaptions(ctx, t);
    drawHook(ctx, ot);
    if (doc.watermark) { ctx.font = `800 46px "Poppins ExtraBold"`; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = "rgba(255,255,255,.6)";
      const cy = capY(); ctx.fillText(doc.watermark.slice(0, 40), W / 2, cy + 128 <= 1660 ? cy + 128 : cy - 140); }
    placeBoxes();
  }
  function geom() {
    const d = E.data, face = d.facecam && d.facecam.length === 4;
    let lay = E.doc.layout;
    if ((lay === "gameplay" || lay === "split") && d.camera === "full") lay = "crop";
    else if ((lay === "gameplay" || lay === "split") && !face) lay = "blur";
    if (d.src_h >= d.src_w && lay !== "crop") lay = "crop";
    const top = (d.splits || {})[E.doc.split] || d.top_h || 720;
    const fg = lay === "blur" ? Math.min(H, Math.round(W * d.src_h / d.src_w / 2) * 2) : lay === "zoom" ? 1000 : H, t0 = (H - fg) / 2;
    let hy, cy;
    if (lay === "split") { hy = top; cy = Math.min(1640, top + Math.floor((H - top) * 0.63)); }
    else if (lay === "crop" || lay === "gameplay") { hy = 300; cy = 1380; }
    else { hy = Math.max(230, t0 - 190); cy = Math.min(1640, t0 + fg + 180); }
    if (lay === "gameplay" && face) hy = d.pip_margin + Math.round(d.pip_w * d.facecam[3] / d.facecam[2]) + 32;
    return {lay, fg, top, hy, cy};
  }
  function hookY() { return E.doc.edit.hook_y || geom().hy; }
  function capY() { return E.doc.edit.cap_y || geom().cy; }
  const libItem = id => E.lib.find(i => i.id === id);
  function hookFam() { const f = libItem(E.doc.edit.hook_font); return f && f.ready ? `PKLIB-${f.id}` : E.data.hook.font; }
  function capFam(st) { const f = libItem(E.doc.edit.cap_font); return f && f.ready ? `PKLIB-${f.id}` : st.font; }
  function roundRect(ctx, x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
  function mediaEl(id) {          // the picture or video of a library item, for drawing
    const it = libItem(id); if (!it) return null;
    if (E.libEl[id]) return E.libEl[id];
    let el;
    if (it.type === "image") { el = new Image(); el.src = it.url; }
    else { el = document.createElement("video"); el.src = it.url; el.muted = it.type === "gif"; el.playsInline = true; el.preload = "auto"; el.loop = it.type === "gif"; }
    E.libEl[id] = el; return el;
  }
  function mediaRect(m, el) {
    const it = libItem(m.id) || {}, w = m.w * W, iw = (el && (el.naturalWidth || el.videoWidth)) || it.w || 16, ih = (el && (el.naturalHeight || el.videoHeight)) || it.h || 9;
    const h = w * ih / iw; return [m.x * W - w / 2, m.y * H - h / 2, w, h];
  }
  function drawMedia(ctx, ot) {
    E.mediaRects = {};
    E.doc.edit.media.forEach((m, i) => {
      const at = toOut(E.doc, m.t); if (at == null) return;
      const el = mediaEl(m.id), it = libItem(m.id); if (!el || !it) return;
      const on = ot >= at && ot <= at + m.d;
      if (it.type !== "image") {      // a video plays along with the clip, from where it was put
        const want = (m.from || 0) + Math.max(0, ot - at);
        if (on) {
          if (Math.abs(el.currentTime - want) > 0.25) { try { el.currentTime = want; } catch {} }
          el.volume = clamp(m.vol * 0.9, 0, 1); el.muted = it.type === "gif" || !it.audio || (E.v && E.v.muted);
          if (E.playing && el.paused) el.play().catch(() => {}); if (!E.playing && !el.paused) el.pause();
        } else if (!el.paused) el.pause();
      }
      if (!on) return;
      const r = mediaRect(m, el); E.mediaRects[i] = r;
      try { ctx.drawImage(el, r[0], r[1], r[2], r[3]); } catch {}
    });
  }
  function hookLines(ctx, text, size) {
    ctx.font = `800 ${size}px "${hookFam()}"`;
    const words = text.split(/\s+/).filter(Boolean), max = E.data.hook.max_w;
    let lines = [words.join(" ")];
    if (ctx.measureText(lines[0]).width > max && words.length > 1) {
      let best = 1, bw = 1e9;
      for (let i = 1; i < words.length; i++) { const w = Math.max(ctx.measureText(words.slice(0, i).join(" ")).width, ctx.measureText(words.slice(i).join(" ")).width); if (w < bw) { bw = w; best = i; } }
      lines = [words.slice(0, best).join(" "), words.slice(best).join(" ")];
    }
    return lines;
  }
  function drawHook(ctx, ot) {
    const doc = E.doc, ed = doc.edit, text = (doc.hook || "").trim();
    if (!text || ot < 0) return;
    const until = ed.hook_dur > 0 ? ed.hook_dur : 1e9;
    if (ot > until) return;
    let size = E.data.hook.px;
    let lines = hookLines(ctx, text, size);
    let widest = Math.max(...lines.map(l => ctx.measureText(l).width));
    if (widest > E.data.hook.max_w) { size = Math.max(40, size * E.data.hook.max_w / widest); lines = hookLines(ctx, text, size); widest = Math.max(...lines.map(l => ctx.measureText(l).width)); }
    const lh = size * 1.16, bw = widest + 72, bh = lh * lines.length + 44, y = hookY();
    let a = clamp(ot / 0.16, 0, 1); if (ed.hook_dur > 0) a = Math.min(a, clamp((until - ot) / 0.22, 0, 1));
    let s = 1;
    if (ed.hook_fx === "pop") { const q = ot * 1000; s = q < 170 ? 0.3 + (1.12 - 0.3) * q / 170 : q < 300 ? 1.12 - 0.12 * (q - 170) / 130 : 1; }
    ctx.save(); ctx.globalAlpha = a; ctx.translate(W / 2, y); ctx.scale(s, s); ctx.translate(-W / 2, -y);
    const boxed = ["box", "pop", "lime", "type"].includes(ed.hook_fx);
    if (boxed) { ctx.fillStyle = ed.hook_fx === "lime" ? "#C6F432" : "#FFFFFF"; roundRect(ctx, (W - bw) / 2, y - bh / 2, bw, bh, Math.min(26, bh / 2)); ctx.fill(); }
    ctx.font = `800 ${size}px "${hookFam()}"`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    const total = lines.join("").length; let shown = 1e9;
    if (ed.hook_fx === "type") { const step = clamp(Math.floor(600 / Math.max(1, total)), 18, 45); shown = Math.floor(ot * 1000 / step); }
    let before = 0;
    lines.forEach((l, i) => {
      const cy = y - lh * (lines.length - 1) / 2 + i * lh + size * 0.04;
      const txt = ed.hook_fx === "type" ? l.slice(0, Math.max(0, shown - before)) : l;
      before += l.length;
      if (ed.hook_fx === "type") { ctx.textAlign = "left"; const lw = ctx.measureText(l).width; ctx.fillStyle = "#14100E"; ctx.fillText(txt, W / 2 - lw / 2, cy); ctx.textAlign = "center"; return; }
      if (ed.hook_fx === "outline") { ctx.lineJoin = "round"; ctx.lineWidth = 18; ctx.strokeStyle = "#000"; ctx.strokeText(txt, W / 2, cy); ctx.fillStyle = "#fff"; ctx.fillText(txt, W / 2, cy); return; }
      if (ed.hook_fx === "glow") { ctx.shadowColor = "#C6F432"; ctx.shadowBlur = 40; ctx.fillStyle = "#C6F432"; ctx.fillText(txt, W / 2, cy); ctx.shadowBlur = 0; ctx.lineWidth = 6; ctx.strokeStyle = "#14100E"; ctx.strokeText(txt, W / 2, cy); ctx.fillStyle = "#fff"; ctx.fillText(txt, W / 2, cy); return; }
      ctx.fillStyle = "#14100E"; ctx.fillText(txt, W / 2, cy);
    });
    ctx.restore();
    E.hookRect = [(W - bw) / 2, y - bh / 2, bw, bh];
  }
  function groups(words, per) {
    const out = []; let cur = [];
    for (const w of words) {
      if (cur.length && (cur.length >= per || cur.reduce((n, x) => n + x.w.length, 0) + w.w.length > 16 || w.s - cur[cur.length - 1].e > 0.45 || /[.?!]$/.test(cur[cur.length - 1].w))) { out.push(cur); cur = []; }
      cur.push(w);
    }
    if (cur.length) out.push(cur);
    return out;
  }
  function capWords() {
    const doc = E.doc, cuts = cutsMerged(doc.edit.cuts, doc.start, doc.end);
    return (E.data.words || []).filter(w => w.s >= doc.start - 0.05 && w.s < doc.end && !cuts.some(([a, b]) => (w.s + w.e) / 2 >= a && (w.s + w.e) / 2 < b));
  }
  function drawCaptions(ctx, t) {
    const doc = E.doc; if (!doc.captions) return;
    const st = E.data.styles[doc.style] || E.data.styles.bounce; if (!st) return;
    if (!E._gcache || E._gkey !== doc.style + JSON.stringify(doc.edit.cuts) + doc.start + doc.end) {
      E._gcache = groups(capWords(), st.per); E._gkey = doc.style + JSON.stringify(doc.edit.cuts) + doc.start + doc.end;
    }
    const gs = E._gcache; let g = null;
    for (let i = 0; i < gs.length; i++) { const end = Math.min(gs[i][gs[i].length - 1].e + 0.25, gs[i + 1] ? gs[i + 1][0].s : 1e9); if (t >= gs[i][0].s && t < end) { g = gs[i]; break; } }
    if (!g) return;
    let size = st.px;
    ctx.font = `${size}px "${capFam(st)}"`; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    const texts = g.map(w => { const x = w.w.replace(/[.,;:]+$/, ""); return st.upper ? x.toUpperCase() : x; });
    const gap = size * 0.28 + st.outline * 2;
    let total = texts.reduce((n, x) => n + ctx.measureText(x).width, 0) + gap * (texts.length - 1);
    if (total > 960) { size = Math.max(36, size * 960 / total); ctx.font = `${size}px "${capFam(st)}"`; total = texts.reduce((n, x) => n + ctx.measureText(x).width, 0) + gap * (texts.length - 1); }
    const cy = capY(); let x = (W - total) / 2;
    if (st.band) { ctx.fillStyle = "rgba(0,0,0,.65)"; roundRect(ctx, (W - total) / 2 - size * .35, cy - size * .66, total + size * .7, size * 1.32, 28); ctx.fill(); }
    g.forEach((w, i) => {
      const txt = texts[i], wd = ctx.measureText(txt).width, said = t >= w.s, now_ = said && (i + 1 < g.length ? t < g[i + 1].s : true);
      let col = st.base; if (now_) col = st.active; else if (said && st.keep) col = st.active;
      ctx.save();
      const cx = x + wd / 2;
      if (now_ && !st.box) { ctx.translate(cx, cy); ctx.scale(1.08, 1.08); ctx.translate(-cx, -cy); }
      if (now_ && st.box) { ctx.fillStyle = st.box; roundRect(ctx, x - size * .13, cy - size * .54, wd + size * .26, size * 1.08, 18); ctx.fill(); }
      if (st.outline && !(now_ && st.box)) { ctx.lineJoin = "round"; ctx.lineWidth = st.outline * 2.2 * size / st.px; ctx.strokeStyle = st.line || "#000"; ctx.strokeText(txt, x, cy); }
      ctx.fillStyle = col; ctx.fillText(txt, x, cy);
      ctx.restore();
      x += wd + gap;
    });
  }
  function placeBoxes() {   // the draggable boxes over the big text and the captions
    const ph = E.root.querySelector("#edPhone"); if (!ph) return;
    const hb = E.root.querySelector("#edHookBox"), cb = E.root.querySelector("#edCapBox"), h = ph.clientHeight / H;
    if (E.doc.hook.trim() && E.hookRect) { hb.style.display = ""; hb.style.top = (E.hookRect[1] * h - 6) + "px"; hb.style.height = (E.hookRect[3] * h + 12) + "px"; hb.classList.toggle("on", E.tool === "text"); }
    else hb.style.display = "none";
    if (E.doc.captions) { cb.style.display = ""; const st = E.data.styles[E.doc.style] || {px: 120}; cb.style.top = ((capY() - st.px * .7) * h) + "px"; cb.style.height = (st.px * 1.4 * h) + "px"; }
    else cb.style.display = "none";
    let mb = E.root.querySelector("#edMediaBox");      // the picked picture or video: drag to move, the corner to resize
    const r = E.sel && E.sel.k === "media" && E.mediaRects && E.mediaRects[E.sel.i];
    if (r) {
      if (!mb) { mb = document.createElement("div"); mb.id = "edMediaBox"; mb.className = "ed-mbox"; mb.innerHTML = "<i></i>"; ph.append(mb); }
      Object.assign(mb.style, {left: r[0] * h + "px", top: r[1] * h + "px", width: r[2] * h + "px", height: r[3] * h + "px", display: ""});
    } else if (mb) mb.style.display = "none";
  }

  // ---------------------------------------------------------------- the timeline
  function fitZoom() {
    const w = E.lane ? E.lane.clientWidth : 800, span = E.doc.end - E.doc.start + 4;
    E.pps = clamp((w - 30) / span, 8, 400);
  }
  const X = t => (t - E.data.p0) * E.pps;
  function lanes() {
    const hgt = (E.root.querySelector(".ed-tracks")?.clientHeight || 200);
    const fixed = 20 + 14;
    const rest = Math.max(120, hgt - fixed), r = n => Math.max(22, Math.floor(rest * n));
    return [["", 20], ["🔥 Hype", 14], ["🎬 Video", r(.27)], ["💬 Captions", r(.15)], ["T  Text", r(.13)], ["🖼 Media", r(.13)], ["✦ Effects", r(.14)], ["🔊 Sound", r(.18)]];
  }
  function drawTimeline() {
    if (!E || !E.data || !E.inner) return;
    const d = E.data, doc = E.doc, ed = doc.edit, L = lanes();
    E.root.querySelector("#edLbls").innerHTML = L.map(([n, h]) => `<div style="height:${h}px">${n}</div>`).join("");
    const width = X(d.p1) + 40, hh = L.map(x => x[1]);
    let html = "";
    // ruler
    const stepS = E.pps > 90 ? 1 : E.pps > 40 ? 2 : E.pps > 18 ? 5 : 10;
    let ru = ""; for (let s = doc.start + Math.ceil((d.p0 - doc.start) / stepS) * stepS; s <= d.p1; s += stepS) ru += `<i style="left:${X(s)}px"></i><span style="left:${X(s) + 3}px">${s < doc.start - 0.01 ? "−" + tc(doc.start - s, false) : tc(s - doc.start, false)}</span>`;
    html += `<div class="ed-ruler" data-seek="1" style="height:${hh[0]}px">${ru}</div>`;
    html += `<div class="ed-hype" style="height:${hh[1]}px"><canvas id="edHypeCv"></canvas></div>`;
    // video
    const vs = E.tw ? `background-image:url(${d.strip});background-size:${d.thumbs * d.step * E.pps}px 100%;background-position:${-0}px 0;` : "";
    let v = `<div class="ed-vid" style="left:0;width:${X(d.p1)}px;${vs}"></div>`;
    const keep = keepOf(doc);
    for (const [a, b] of keep) v += `<div class="ed-vidin" style="left:${X(a)}px;width:${X(b) - X(a)}px;${vs}background-position:${-X(a)}px 0"></div>`;
    // segments between splits / cuts, selectable
    // the parts between splits, selectable (a click on the track picks one only once the clip has been split)
    const sp = (doc.splits || []).filter(s => s > doc.start && s < doc.end);
    const bounds = sp.length ? [...new Set([doc.start, doc.end, ...sp])].sort((a, b) => a - b) : [];
    for (let i = 0; i < bounds.length - 1; i++) {
      const a = bounds[i], b = bounds[i + 1]; if (b - a < 0.05 || inCut(doc, (a + b) / 2)) continue;
      v += `<div class="ed-seg2${E.sel && E.sel.k === "seg" && Math.abs(E.sel.a - a) < 1e-3 ? " sel" : ""}" data-tseg="${a},${b}" style="left:${X(a)}px;width:${X(b) - X(a)}px"></div>`;
    }
    cutsMerged(ed.cuts, doc.start, doc.end).forEach(([a, b], i) => v += `<div class="ed-cut${E.sel && E.sel.k === "cut" && E.sel.i === i ? " sel" : ""}" data-cut="${i}" title="Cut out (${(b - a).toFixed(1)} s). Drag an edge to change it; click, then Delete to bring it back" style="left:${X(a)}px;width:${Math.max(4, X(b) - X(a))}px">${X(b) - X(a) > 52 ? `<em>✂ ${(b - a).toFixed(1)}s</em>` : ""}</div>`);
    (doc.splits || []).forEach(s => { if (s > doc.start && s < doc.end) v += `<i class="ed-split" style="left:${X(s)}px"></i>`; });
    v += `<div class="ed-clipr" style="left:${X(doc.start)}px;width:${X(doc.end) - X(doc.start)}px"></div>`;
    if (E.sel && E.sel.k === "range") v += `<div class="ed-rng" style="left:${X(E.sel.a)}px;width:${Math.max(2, X(E.sel.b) - X(E.sel.a))}px"><span>${(E.sel.b - E.sel.a).toFixed(1)} s</span></div>`;
    v += `<div class="ed-handle" data-h="start" title="Drag to change where the clip starts" style="left:${X(doc.start) - 6}px"></div><div class="ed-handle" data-h="end" title="Drag to change where the clip ends" style="left:${X(doc.end) - 6}px"></div>`;
    ed.fx.forEach((f, i) => v += `<div class="ed-tr${E.sel && E.sel.k === "fx" && E.sel.i === i ? " sel" : ""}" data-fx="${i}" title="${esc(d.options.transitions[f.type])}" style="left:${X(f.t)}px"></div>`);
    html += `<div class="ed-ln ed-vl" style="height:${hh[2]}px">${v}</div>`;
    // captions
    const cutW = w => cutsMerged(ed.cuts, doc.start, doc.end).some(([a, b]) => (w.s + w.e) / 2 >= a && (w.s + w.e) / 2 < b);
    let cw = "";
    for (const w of d.words) { const wx = X(w.s), ww = Math.max(6, X(w.e) - wx - 2);
      cw += `<span class="ed-word${cutW(w) ? " cut" : ""}${w.s < doc.start || w.s >= doc.end ? " " : ""}" data-word="${w.s}" style="left:${wx}px;width:${ww}px;${w.s < doc.start || w.s >= doc.end ? "opacity:.35;" : ""}${ww < 30 ? "font-size:9px;padding:0 2px" : ""}">${ww > 13 ? esc(w.w) : ""}</span>`; }
    html += `<div class="ed-ln ed-cl" style="height:${hh[3]}px">${E.sel && E.sel.k === "range" ? `<div class="ed-rng" style="left:${X(E.sel.a)}px;width:${Math.max(2, X(E.sel.b) - X(E.sel.a))}px"></div>` : ""}${doc.captions ? cw : `<span style="position:absolute;left:${X(doc.start) + 8}px;top:8px;color:var(--faint);font-size:11px">Captions are off</span>`}</div>`;
    // text
    const hs = doc.start, he = ed.hook_dur > 0 ? fromOut(doc, Math.min(outLen(doc), ed.hook_dur)) : doc.end;
    html += `<div class="ed-ln" style="height:${hh[4]}px">${doc.hook.trim() ? `<div class="ed-txt${E.sel && E.sel.k === "hook" ? " sel" : ""}" data-hook="1" style="left:${X(hs)}px;width:${Math.max(30, X(he) - X(hs))}px">${esc(doc.hook)} · ${esc(d.options.hook_fx[ed.hook_fx])}</div>` : ""}</div>`;
    // media: pictures, GIFs and videos from the library, over the clip
    let md = "";
    ed.media.forEach((m, i) => { const it = libItem(m.id); md += `<div class="ed-med${E.sel && E.sel.k === "media" && E.sel.i === i ? " sel" : ""}" data-media="${i}" style="left:${X(m.t)}px;width:${Math.max(30, m.d * E.pps)}px">${it && it.thumb ? `<img src="${it.thumb}" alt="">` : ""}${esc(it ? it.name : "Deleted")}</div>`; });
    html += `<div class="ed-ln" style="height:${hh[5]}px">${md}</div>`;
    // effects: sound effects and punch-ins
    let ef = "";
    ed.punch.forEach((p, i) => ef += `<div class="ed-pun${E.sel && E.sel.k === "punch" && E.sel.i === i ? " sel" : ""}" data-punch="${i}" style="left:${X(p.t)}px;width:${Math.max(26, p.d * E.pps)}px">⤢ ${p.z.toFixed(2)}×</div>`);
    html += `<div class="ed-ln" style="height:${hh[6]}px">${ef}</div>`;
    let sf = `<canvas class="ed-wave" id="edWaveCv"></canvas>`;
    ed.sfx.forEach((s, i) => sf += `<div class="ed-sfx${E.sel && E.sel.k === "sfx" && E.sel.i === i ? " sel" : ""}" data-sfx="${i}" style="left:${X(s.t)}px">${SFX_ICON[s.kind] || "♪"} ${esc(sfxName(s.kind))}</div>`);
    html += `<div class="ed-ln" style="height:${hh[7]}px">${sf}</div>`;
    html += `<div class="ed-ph" id="edPh"></div>`;
    // the hottest moments, marked on the ruler
    for (const h of hotMoments(3)) html += `<span class="ed-hot" style="left:${X(h)}px;top:${hh[0] - 2}px">🔥</span>`;
    E.inner.style.width = width + "px";
    E.inner.innerHTML = html;
    drawCurves(width, hh);
    tickUi();
    const del = E.root.querySelector("#edDel"); if (del) del.disabled = !E.sel;
  }
  function drawCurves(width, hh) {
    const d = E.data, dpr = Math.min(2, devicePixelRatio || 1);
    const hc = E.root.querySelector("#edHypeCv");
    if (hc) { hc.width = width * dpr; hc.height = hh[1] * dpr; const c = hc.getContext("2d"); c.scale(dpr, dpr);
      const n = d.hype.length, step = (d.p1 - d.p0) / Math.max(1, n);
      for (let i = 0; i < n; i++) { const v = d.hype[i]; c.fillStyle = `hsla(${70 - v * 60},95%,${30 + v * 30}%,${0.25 + v * 0.75})`; c.fillRect(i * step * E.pps, 0, Math.ceil(step * E.pps) + 1, hh[1]); } }
    const wc = E.root.querySelector("#edWaveCv");
    if (wc) { const h = hh[7] - 6; wc.width = width * dpr; wc.height = h * dpr; wc.style.width = width + "px"; wc.style.height = h + "px"; const c = wc.getContext("2d"); c.scale(dpr, dpr);
      const n = d.peaks.length, step = (d.p1 - d.p0) / Math.max(1, n), doc = E.doc;
      for (let i = 0; i < n; i++) { const t = d.p0 + i * step, inside = t >= doc.start && t < doc.end && !inCut(doc, t), v = d.peaks[i];
        c.fillStyle = inside ? "rgba(198,244,50,.85)" : "rgba(255,255,255,.18)"; const bh = Math.max(1, v * h * 0.95); c.fillRect(i * step * E.pps, (h - bh) / 2, Math.max(1, step * E.pps - 0.5), bh); } }
  }
  function hotMoments(n, d = E.data, doc = E.doc) {      // the hype curve's highest moments inside the clip, a few seconds apart
    if (!d || !d.hype.length) return [];
    const step = (d.p1 - d.p0) / d.hype.length, idx = d.hype.map((v, i) => [v, d.p0 + i * step]).filter(([, t]) => t > doc.start + 0.8 && t < doc.end - 1 && !inCut(doc, t)).sort((a, b) => b[0] - a[0]);
    const out = [];
    for (const [v, t] of idx) { if (v < 0.55 || out.length >= n) break; if (out.every(q => Math.abs(q - t) > 4)) out.push(t); }
    return out.sort((a, b) => a - b);
  }
  function tickUi() {
    if (!E || !E.data) return;
    const t = now(), doc = E.doc, ph = E.root.querySelector("#edPh");
    if (ph) ph.style.left = X(t) + "px";
    const o = toOut(doc, t), el = E.root.querySelector("#edTc");
    if (el) el.textContent = `${tc(o == null ? 0 : o)} / ${tc(outLen(doc))}`;
    if (E.playing && E.lane) {        // the timeline follows the playhead
      const x = X(t), l = E.lane.scrollLeft, w = E.lane.clientWidth;
      if (x > l + w - 60 || x < l) E.lane.scrollLeft = x - 80;
    }
    const tr = E.root.querySelector("#edTrans");
    if (tr) { const cur = tr.querySelector("span.now"); const w = (E.data.words || []).find(w => t >= w.s && t < w.e); const nx = w ? tr.querySelector(`[data-word="${w.s}"]`) : null;
      if (cur !== nx) { cur && cur.classList.remove("now"); nx && nx.classList.add("now"); } }
    E.root.querySelectorAll(".ed-word.now").forEach(x => x.classList.remove("now"));
  }

  // ---------------------------------------------------------------- the left panel, one per tool
  function tile(type, on, title) {
    return `<button class="ed-tile${on ? " on" : ""}" data-addtr="${type}" title="${esc(TR_HINT[type] || "")}"><div class="pv ${type}"><b>${TR_ICON[type]}</b></div>${esc(title)}</button>`;
  }
  function frameThumb() { return E.strip && E.tw ? `background-image:url(${E.data.strip});background-size:${E.data.thumbs * 100}% 100%;background-position:${clamp((E.doc.start + 1 - E.data.p0) / E.data.step / Math.max(1, E.data.thumbs - 1) * 100, 0, 100)}% 0` : ""; }
  function drawPanel() {
    if (!E) return;
    const p = E.root.querySelector("#edPanel"), doc = E.doc, ed = doc.edit, o = E.data ? E.data.options : null;
    E.root.querySelectorAll(".ed-rail [data-tool]").forEach(b => b.classList.toggle("on", b.dataset.tool === E.tool));
    if (!o) { p.innerHTML = `<div class="ed-help" style="margin-top:4px">Loading…</div>`; return; }
    let h = "";
    if (E.tool === "edit") {
      const words = E.data.words, cuts = cutsMerged(ed.cuts, doc.start, doc.end);
      const isCut = w => cuts.some(([a, b]) => (w.s + w.e) / 2 >= a && (w.s + w.e) / 2 < b);
      const fill = words.filter(w => w.s >= doc.start && w.s < doc.end && FILLER_SAFE.test(w.w.replace(/[^\w.,!?…]/g, "")) && !isCut(w)).length;
      h += `<div class="ed-sec"><div class="ed-h">Edit by transcript <em>Like a doc</em></div>
        <p class="ed-help">Click a word to jump to it. Drag across words (or Shift-click) and press <span class="ed-kbd">Del</span> to cut them out of the video.</p>
        <div class="ed-trans" id="edTrans">${words.length ? words.map(w => `<span data-word="${w.s}" class="${isCut(w) ? "cut" : ""}${w.s < doc.start || w.s >= doc.end ? " out" : ""}${E.tsel.includes(w.s) ? " sel" : ""}">${esc(w.w)}</span>`).join(" ") : `<span style="color:var(--faint)">The words show here once this clip has been made with captions on.</span>`}</div></div>
        <div class="ed-sec"><div class="ed-h">Clean up</div>
        <div class="ed-row"><div><b>Remove filler words</b><span>${fill ? `${fill} found: um, uh, hmm` : "None left in this clip"}</span></div><button class="ed-btn sm" data-e="fillers" ${fill ? "" : "disabled"}>Remove</button></div>
        <div class="ed-row"><div><b>Cut silences</b><span>Quiet stretches are shortened when you export</span></div><button class="ed-tg${doc.tighten ? " on" : ""}" data-tg="tighten"></button></div>
        ${cuts.length ? `<div class="ed-row"><div><b>${cuts.length} cut${cuts.length > 1 ? "s" : ""}</b><span>${(cuts.reduce((t, [a, b]) => t + b - a, 0)).toFixed(1)} s taken out</span></div><button class="ed-btn sm" data-e="uncut">Bring back all</button></div>` : ""}</div>
        <div class="ed-sec"><div class="ed-h">Clip length</div>
        <div class="ed-grid2"><label class="ed-f"><span>Starts at</span><input class="ed-in" type="number" step="0.1" data-num="start" value="${(doc.start - E.data.p0).toFixed(1)}"></label>
        <label class="ed-f"><span>Ends at</span><input class="ed-in" type="number" step="0.1" data-num="end" value="${(doc.end - E.data.p0).toFixed(1)}"></label></div>
        <p class="ed-help">Or drag the lime handles on the timeline. ${E.data.p1 - E.data.p0 > 0 ? `You can reach ${Math.round(doc.start - E.data.p0)} s before and ${Math.round(E.data.p1 - doc.end)} s after it.` : ""}</p></div>`;
    } else if (E.tool === "frame") {
      const L = (S.status && S.status.layouts) || {}, pic = {blur: [[34, 32]], zoom: [[28, 44]], crop: [[0, 100]], gameplay: [[0, 100], [3, 14, "pip"]], split: [[0, 38], [40, 60]]};
      h += `<div class="ed-sec"><div class="ed-h">Layout</div><div class="ed-layouts">${Object.entries(L).map(([k, v]) => `<button data-layout="${k}" class="${doc.layout === k ? "on" : ""}" title="${esc(v)}"><span class="ed-lay">${(pic[k] || []).map(([t, hh, pip]) => pip ? `<i style="top:${t}%;height:${hh}%;left:auto;right:3px;width:40%;background:#C6F432"></i>` : `<i style="top:${t}%;height:${hh}%"></i>`).join("")}</span>${esc(v.split(/[,+(]/)[0].trim())}</button>`).join("")}</div>
        ${E.data.layout !== doc.layout && ["split", "gameplay"].includes(doc.layout) && !E.data.facecam ? `<p class="ed-help" style="margin-top:8px">No webcam was found yet: press Fix facecam to show where it is.</p>` : ""}
        ${doc.layout === "split" && E.data.facecam ? `<div class="ed-h" style="margin-top:14px">Facecam size</div><div class="ed-seg">${[["small", "Small"], ["balanced", "Balanced"], ["half", "Half"], ["big", "Big"]].map(([k, l]) => `<button data-split="${k}" class="${doc.split === k ? "on" : ""}">${l}</button>`).join("")}</div>` : ""}</div>
        <div class="ed-sec"><div class="ed-h">Facecam</div><div class="ed-row"><div><b>Fix the facecam</b><span>Draw a box around the streamer's webcam</span></div><button class="ed-btn sm" data-e="camfix">${eic("eye")}Fix</button></div></div>
        <div class="ed-sec"><div class="ed-h">Motion</div>
        <div class="ed-row"><div><b>Auto punch-in</b><span>Zooms in on the loudest moments when you export</span></div><button class="ed-tg${doc.zoom ? " on" : ""}" data-tg="zoom"></button></div>
        <div class="ed-row"><div><b>Punch in here</b><span>At the playhead, for a second</span></div><button class="ed-btn sm" data-e="addpunch">⤢ Add</button></div></div>
        <div class="ed-sec"><div class="ed-h">Where things sit</div>
        <label class="ed-f"><span>Big text <b>${hookY()}</b></span><input class="ed-range" type="range" min="120" max="1750" step="5" data-range="hook_y" value="${hookY()}"></label>
        <label class="ed-f"><span>Captions <b>${capY()}</b></span><input class="ed-range" type="range" min="300" max="1750" step="5" data-range="cap_y" value="${capY()}"></label>
        <p class="ed-help">Or drag them on the preview. <button class="ed-btn sm" data-e="resetpos" style="margin-left:4px">Reset</button></p>
        <div class="ed-row"><div><b>Safe zone</b><span>Where the app's buttons cover the video</span></div><button class="ed-tg${E.safe ? " on" : ""}" data-e="safe"></button></div></div>`;
    } else if (E.tool === "captions") {
      h += `<div class="ed-sec"><div class="ed-row"><div><b>Captions</b><span>Word by word, timed to the voice</span></div><button class="ed-tg${doc.captions ? " on" : ""}" data-tg="captions"></button></div></div>
        <div class="ed-sec"><div class="ed-h">Style <em>${esc((E.data.styles[doc.style] || {}).label || "")}</em></div><div class="ed-styles">${Object.entries(E.data.styles).map(([k, s]) => `<button data-style="${k}" class="${doc.style === k ? "on" : ""}"><img src="/brand/styles/${k}.jpg" alt="" loading="lazy" onerror="this.remove()"><span>${esc(s.label)}</span></button>`).join("")}</div></div>
        <div class="ed-sec"><div class="ed-h">Position</div><label class="ed-f"><span>From the top <b>${capY()}</b></span><input class="ed-range" type="range" min="300" max="1750" step="5" data-range="cap_y" value="${capY()}"></label></div>
        <div class="ed-sec"><div class="ed-h">Font <em>Add your own in Library</em></div><div class="ed-fonts">${fontChoices("cap_font")}</div></div>`;
    } else if (E.tool === "text") {
      h += `<div class="ed-sec"><div class="ed-h">Big text</div><label class="ed-f"><input class="ed-in" data-txt="hook" maxlength="60" value="${esc(doc.hook)}" placeholder="The hook that stops the scroll"></label>
        <div class="ed-h" style="margin-top:6px">Effect</div><div class="ed-txfx">${Object.entries(o.hook_fx).map(([k, v]) => `<button data-hookfx="${k}" class="${ed.hook_fx === k ? "on" : ""}" style="${{box: "background:#fff;color:#14100E", pop: "background:#fff;color:#14100E;transform:scale(.96)", lime: "background:#C6F432;color:#14100E", outline: "color:#fff;text-shadow:2px 2px 0 #000,-2px -2px 0 #000,2px -2px 0 #000,-2px 2px 0 #000;background:#4a3a30", glow: "color:#fff;text-shadow:0 0 10px #C6F432,0 0 18px #C6F432", type: "letter-spacing:.14em;font-family:'Geist Mono',monospace;font-weight:700"}[k]}">${esc(v)}</button>`).join("")}</div></div>
        <div class="ed-sec"><div class="ed-h">Font <em>Add your own in Library</em></div><div class="ed-fonts">${fontChoices("hook_font")}</div></div>
        <div class="ed-sec"><div class="ed-h">How long it stays</div><div class="ed-seg">${[[0, "Whole clip"], [3, "3 s"], [5, "5 s"], [8, "8 s"]].map(([v, l]) => `<button data-hookdur="${v}" class="${(ed.hook_dur || 0) === v ? "on" : ""}">${l}</button>`).join("")}</div></div>
        <div class="ed-sec"><div class="ed-h">Position</div><label class="ed-f"><span>From the top <b>${hookY()}</b></span><input class="ed-range" type="range" min="120" max="1750" step="5" data-range="hook_y" value="${hookY()}"></label><p class="ed-help">Or drag it on the preview.</p></div>
        <div class="ed-sec"><div class="ed-h">Your @handle</div><label class="ed-f"><input class="ed-in" data-txt="watermark" maxlength="40" value="${esc(doc.watermark)}" placeholder="@yourname"></label><p class="ed-help">Small, under the captions, so reposts still point to you.</p></div>`;
    } else if (E.tool === "effects") {
      h += `<div class="ed-sec"><div class="ed-h">Transitions <em>On a cut or anywhere</em></div><p class="ed-help">Click one to add it at the playhead (it snaps to a cut close by). Drag the ◆ on the timeline to move it.</p>
        <div class="ed-grid3">${Object.entries(o.transitions).map(([k, v]) => tile(k, E.sel && E.sel.k === "fx" && ed.fx[E.sel.i] && ed.fx[E.sel.i].type === k, v)).join("")}</div></div>
        <div class="ed-sec"><div class="ed-h">Look</div><div class="ed-looks">${Object.entries(o.looks).map(([k, v]) => `<button data-look="${k}" class="${ed.look === k ? "on" : ""}" style="${frameThumb()};filter:${v.css}"><span>${esc(v.label)}</span></button>`).join("")}</div></div>
        <div class="ed-sec"><div class="ed-h">Motion</div><div class="ed-row"><div><b>Punch in here</b><span>The picture pushes in at the playhead</span></div><button class="ed-btn sm" data-e="addpunch">⤢ Add</button></div>
        <div class="ed-row"><div><b>Transition on every cut</b><span>${joins(doc).length} cut${joins(doc).length === 1 ? "" : "s"} in this clip</span></div><button class="ed-btn sm" data-e="everycut" ${joins(doc).length ? "" : "disabled"}>Whoosh all</button></div></div>`;
    } else if (E.tool === "audio") {
      h += `<div class="ed-sec"><div class="ed-h">Sound effects <em>Made by PapiClips · free to use</em></div><p class="ed-help">▶ to hear it, + to add it at the playhead. Whooshes and risers start just before, so they land on the moment.</p>
        <div class="ed-chips">${Object.entries(o.sounds).map(([k, v]) => `<span class="ed-chip"><span>${SFX_ICON[k] || "♪"} ${esc(v)}</span><button class="pl" data-hear="${k}" title="Hear it">▶</button><button class="pl" data-addsfx="${k}" title="Add at the playhead">+</button></span>`).join("")}</div></div>
        <div class="ed-sec"><div class="ed-h">Stream sound</div>
          <label class="ed-f"><span>Volume <b>${Math.round((ed.voice == null ? 1 : ed.voice) * 100)}%</b></span><input class="ed-range" type="range" min="0" max="1.5" step="0.05" data-range="voice" value="${ed.voice == null ? 1 : ed.voice}"></label>
          <p class="ed-help">Turn it down under music, or to 0% for a meme or music edit.</p>
          <div class="ed-row"><div><b>Cut silences</b><span>Quiet stretches are shortened</span></div><button class="ed-tg${doc.tighten ? " on" : ""}" data-tg="tighten"></button></div>
        <p class="ed-help">Every clip's sound is evened out to the loudness Shorts, Reels and TikTok expect.</p></div>
        <div class="ed-sec"><div class="ed-h">Your sounds <em>${E.lib.filter(i => i.kind === "sound").length}</em></div>${libSounds() || `<p class="ed-help">Add your own sounds (memes, bass drops, your intro) in Library.</p>`}</div>`;
    } else if (E.tool === "library") {
      h += `<div class="ed-drop" data-e="upload" id="edDrop"><b>${E.uploading ? "Uploading " + esc(E.uploading) + "…" : "Add your own files"}</b>Fonts (.ttf .otf), sounds (.mp3 .wav .m4a) and pictures, GIFs or videos for memes. Drop them here or click. They stay in your library for every clip.</div>
        <div class="ed-sec"><div class="ed-h">Pictures, GIFs and videos <em>${E.lib.filter(i => i.kind === "media").length}</em></div>
        ${E.lib.some(i => i.kind === "media") ? `<div class="ed-lib">${E.lib.filter(i => i.kind === "media").map(i => `<div class="it" title="${esc(i.name)}">${i.thumb ? `<img src="${i.thumb}" alt="">` : ""}<span>${i.type === "image" ? "PIC" : i.type === "gif" ? "GIF" : tc(i.dur || 0, false)}</span><div class="acts"><button data-libadd="${i.id}" title="Put it on the clip at the playhead">+</button><button data-libdel="${i.id}" title="Delete from your library">×</button></div></div>`).join("")}</div>` : `<p class="ed-help">Memes, reaction pictures, a logo, a second clip: put them over your clip at any moment.</p>`}</div>
        <div class="ed-sec"><div class="ed-h">Sounds <em>${E.lib.filter(i => i.kind === "sound").length}</em></div>${libSounds() || `<p class="ed-help">Your own sound effects show here.</p>`}</div>
        <div class="ed-sec"><div class="ed-h">Fonts <em>${E.lib.filter(i => i.kind === "font").length}</em></div>${E.lib.filter(i => i.kind === "font").map(i => `<div class="ed-font"><b style="font-family:'PKLIB-${i.id}'">${esc(i.family || i.name)}</b><button class="ed-btn sm" data-font="hook_font" data-v="${i.id}">Big text</button><button class="ed-btn sm" data-font="cap_font" data-v="${i.id}">Captions</button><button class="ed-ib" data-libdel="${i.id}" title="Delete">×</button></div>`).join("") || `<p class="ed-help">Add a font and use it for the big text or the captions.</p>`}</div>`;
    } else if (E.tool === "post") {
      const job = jobOf(E.jid), c = job.clips.find(x => x.id === E.cid);
      h += `<div class="ed-sec"><div class="ed-h">Post text</div><label class="ed-f"><span>Title</span><input class="ed-in" data-txt="title" maxlength="100" value="${esc(doc.title)}"></label>
        <label class="ed-f"><span>Description</span><textarea class="ed-in" data-txt="description" rows="4">${esc(doc.description)}</textarea></label></div>
        <div class="ed-sec"><div class="ed-h">Post it</div>${S.accounts && S.accounts.length ? S.accounts.map(a => `<label class="ed-row" style="cursor:pointer"><div><b>${esc(a.name)}</b><span>${esc(a.label)}</span></div><input type="checkbox" data-pacc value="${esc(a.id)}"></label>`).join("") +
          `<div style="display:flex;gap:8px;flex-wrap:wrap"><button class="ed-btn p sm" data-e="postnow">${eic("send")}Post now</button><button class="ed-btn sm" data-e="postqueue">Add to queue</button></div><p class="ed-help" style="margin-top:8px">Posts the last exported version. Export first if you changed something.</p>`
          : `<p class="ed-help">No accounts connected yet. Connect YouTube, Instagram or TikTok in Settings.</p>`}</div>
        <div class="ed-sec"><div class="ed-h">More</div><div style="display:flex;gap:8px;flex-wrap:wrap">
          ${c.status === "done" ? `<a class="ed-btn sm" href="/media/${E.jid}/${esc(c.file)}?download=1" style="text-decoration:none">${eic("down")}Download</a>` : ""}
          <button class="ed-btn sm" data-act="copy" data-j="${E.jid}" data-c="${esc(E.cid)}">${eic("copy")}Copy post text</button>
          <button class="ed-btn sm" data-act="tocampaign" data-j="${E.jid}" data-c="${esc(E.cid)}">${eic("money")}Send to a campaign</button></div></div>`;
    } else if (E.tool === "ai") {
      const job = jobOf(E.jid), c = job.clips.find(x => x.id === E.cid), hot = hotMoments(5);
      h += `<div class="ed-sec"><div class="ed-h">Hype Edit <em>One click</em></div>
        <p class="ed-help">Reads this clip's hype: how loud the streamer got and how hard chat reacted. Then it cuts the filler words, punches in on the biggest moments, lands a flash and a boom on the peak, adds a riser before it, whooshes the cuts and gives it a punchy look. Everything stays editable, and Undo takes it all back.</p>
        <button class="ed-btn ai" data-e="hype" style="width:100%;justify-content:center;height:42px">${eic("sparkle")}Run Hype Edit</button></div>
        <div class="ed-sec"><div class="ed-h">Hype moments <em>${hot.length}</em></div>${hot.length ? hot.map((t, i) => `<div class="ed-row" style="cursor:pointer" data-go="${t}"><div><b>🔥 Moment ${i + 1}</b><span>${tc(toOut(doc, t) || 0)} into the clip</span></div><span class="ed-kbd">Jump</span></div>`).join("") : `<p class="ed-help">No standout moment: the energy is even through this clip.</p>`}</div>
        ${c.reason ? `<div class="ed-sec"><div class="ed-h">Why the AI picked it</div><p class="ed-help" style="margin:0">${esc(c.reason)}</p></div>` : ""}`;
    }
    p.innerHTML = h;
  }

  function fontChoices(key) {
    const cur = E.doc.edit[key] || "";
    const base = key === "hook_font" ? E.data.hook.font : (E.data.styles[E.doc.style] || {}).font;
    return `<button data-font="${key}" data-v="" class="${cur ? "" : "on"}" style="font-family:'${esc(base || "")}'">Style's own</button>` +
      E.lib.filter(i => i.kind === "font").map(i => `<button data-font="${key}" data-v="${i.id}" class="${cur === i.id ? "on" : ""}" style="font-family:'PKLIB-${i.id}'">${esc(i.family || i.name)}</button>`).join("");
  }
  function libSounds() {
    const ss = E.lib.filter(i => i.kind === "sound");
    return ss.length ? `<div class="ed-chips">${ss.map(i => `<span class="ed-chip"><span>♪ ${esc(i.name)}</span><button class="pl" data-hear="lib:${i.id}" title="Hear it">▶</button><button class="pl" data-addsfx="lib:${i.id}" title="Add at the playhead">+</button><button class="pl" data-music="${i.id}" title="Music under the whole clip">♫</button>${E.tool === "library" ? `<button class="pl" data-libdel="${i.id}" title="Delete">×</button>` : ""}</span>`).join("")}</div>` : "";
  }
  async function loadLibrary() {
    try { const r = await api("/api/library"); if (!E) return; E.lib = r.items || []; } catch { return; }
    await Promise.all(E.lib.filter(i => i.kind === "font").map(i => new FontFace(`PKLIB-${i.id}`, `url(${i.url})`).load().then(f => { document.fonts.add(f); i.ready = true; }).catch(() => {})));
    if (E) { drawPanel(); drawTimeline(); drawFrame(); }
  }
  async function upload(files) {
    const kindOf = n => /\.(ttf|otf)$/i.test(n) ? "font" : /\.(mp3|wav|m4a|aac|ogg|opus|flac)$/i.test(n) ? "sound" : "media";
    for (const f of files) {
      E.uploading = f.name; drawPanel();
      const fd = new FormData(); fd.append("kind", kindOf(f.name)); fd.append("file", f);
      try {
        const r = await fetch("/api/library", {method: "POST", body: fd});
        if (!r.ok) { let m = "That file couldn't be added."; try { m = (await r.json()).detail || m; } catch {} toast(`${f.name}: ${m}`, "bad"); }
      } catch { toast(`${f.name} couldn't be uploaded.`, "bad"); }
    }
    E.uploading = ""; await loadLibrary(); toast("Added to your library.", "ok");
  }
  function addMedia(id) {
    const it = libItem(id); if (!it) return;
    const doc = E.doc, t = r3(clamp(now(), doc.start, doc.end - 0.3));
    const d = it.type === "image" ? 2.5 : Math.min(it.dur || 3, Math.max(0.5, doc.end - t));
    doc.edit.media.push({id, t, d: r3(d), x: 0.5, y: it.type === "image" ? 0.32 : 0.5, w: it.type === "image" ? 0.55 : 0.8, vol: 1, from: 0});
    doc.edit.media.sort((a, b) => a.t - b.t);
    E.sel = {k: "media", i: doc.edit.media.findIndex(m => m.id === id && m.t === t)};
    commit("Media");
  }

  // ---------------------------------------------------------------- the right panel: what's selected
  function drawInsp() {
    if (!E) return;
    const el = E.root.querySelector("#edInsp"), doc = E.doc, ed = doc.edit, o = E.data && E.data.options;
    if (!el) return;
    const s = E.sel;
    if (s && o) {
      if (s.k === "fx" && ed.fx[s.i]) {
        const f = ed.fx[s.i];
        el.innerHTML = `<div class="ed-sel"><i>${TR_ICON[f.type]}</i><div><b>${esc(o.transitions[f.type])}</b><span>At ${tc(toOut(doc, f.t) || 0)}</span></div></div>
          <div class="ed-h">Type</div><div class="ed-grid3" style="margin-bottom:14px">${Object.entries(o.transitions).map(([k, v]) => `<button class="ed-tile${f.type === k ? " on" : ""}" data-settr="${k}" style="padding:6px 4px"><div class="pv ${k}" style="height:28px;margin-bottom:4px"><b style="font-size:14px">${TR_ICON[k]}</b></div>${esc(v)}</button>`).join("")}</div>
          <label class="ed-f"><span>Length <b>${f.d.toFixed(2)} s</b></span><input class="ed-range" type="range" min="0.1" max="1.5" step="0.05" data-fxd value="${f.d}"></label>
          <div class="ed-row"><div><b>Its sound</b><span>${o.transition_sound[f.type] ? esc(o.sounds[o.transition_sound[f.type]]) + ", in sync" : "None for this one"}</span></div><button class="ed-tg${f.sound !== false && o.transition_sound[f.type] ? " on" : ""}" data-fxsound ${o.transition_sound[f.type] ? "" : "disabled"}></button></div>
          <div class="ed-row"><div><b>Use on every cut</b><span>${joins(doc).length} cut${joins(doc).length === 1 ? "" : "s"}</span></div><button class="ed-btn sm" data-e="allcuts" ${joins(doc).length ? "" : "disabled"}>Apply</button></div>
          <button class="ed-btn sm" data-e="del" style="width:100%;justify-content:center;margin-top:6px">${eic("trash")}Remove transition</button>`;
        return;
      }
      if (s.k === "sfx" && ed.sfx[s.i]) {
        const x = ed.sfx[s.i];
        el.innerHTML = `<div class="ed-sel"><i>${SFX_ICON[x.kind] || "♪"}</i><div><b>${esc(sfxName(x.kind))}</b><span>At ${tc(toOut(doc, x.t) || 0)}</span></div></div>
          <div class="ed-h">Sound</div><div class="ed-chips" style="margin-bottom:14px">${Object.entries(o.sounds).concat(E.lib.filter(i => i.kind === "sound").map(i => ["lib:" + i.id, i.name])).map(([k, v]) => `<button class="ed-chip${x.kind === k ? " on" : ""}" data-setsfx="${k}" style="padding:0 10px">${SFX_ICON[k] || "♪"} ${esc(v)}</button>`).join("")}</div>
          <label class="ed-f"><span>Volume <b>${Math.round(x.vol * 100)}%</b></span><input class="ed-range" type="range" min="0.1" max="1.5" step="0.05" data-sfxvol value="${x.vol}"></label>
          <div style="display:flex;gap:8px"><button class="ed-btn sm" data-hear="${x.kind}">▶ Hear it</button><button class="ed-btn sm" data-e="del">${eic("trash")}Remove</button></div>`;
        return;
      }
      if (s.k === "punch" && ed.punch[s.i]) {
        const x = ed.punch[s.i];
        el.innerHTML = `<div class="ed-sel"><i>⤢</i><div><b>Punch-in</b><span>At ${tc(toOut(doc, x.t) || 0)}</span></div></div>
          <label class="ed-f"><span>Zoom <b>${x.z.toFixed(2)}×</b></span><input class="ed-range" type="range" min="1.05" max="1.8" step="0.01" data-punz value="${x.z}"></label>
          <label class="ed-f"><span>Holds for <b>${x.d.toFixed(1)} s</b></span><input class="ed-range" type="range" min="0.3" max="6" step="0.1" data-pund value="${x.d}"></label>
          <button class="ed-btn sm" data-e="del" style="width:100%;justify-content:center">${eic("trash")}Remove punch-in</button>`;
        return;
      }
      if (s.k === "media" && ed.media[s.i]) {
        const m = ed.media[s.i], it = libItem(m.id) || {name: "Deleted", type: "image"};
        el.innerHTML = `<div class="ed-sel"><i>🖼</i><div><b>${esc(it.name)}</b><span>At ${tc(toOut(doc, m.t) || 0)} · drag it on the preview</span></div></div>
          <label class="ed-f"><span>Size <b>${Math.round(m.w * 100)}%</b></span><input class="ed-range" type="range" min="0.05" max="1.6" step="0.01" data-med="w" value="${m.w}"></label>
          <label class="ed-f"><span>Left / right <b>${Math.round(m.x * 100)}</b></span><input class="ed-range" type="range" min="0" max="1" step="0.01" data-med="x" value="${m.x}"></label>
          <label class="ed-f"><span>Up / down <b>${Math.round(m.y * 100)}</b></span><input class="ed-range" type="range" min="0" max="1" step="0.01" data-med="y" value="${m.y}"></label>
          <label class="ed-f"><span>Shows for <b>${m.d.toFixed(1)} s</b></span><input class="ed-range" type="range" min="0.2" max="${Math.max(1, it.type === "image" ? 30 : (it.dur || 10))}" step="0.1" data-med="d" value="${m.d}"></label>
          ${it.audio ? `<label class="ed-f"><span>Its sound <b>${Math.round(m.vol * 100)}%</b></span><input class="ed-range" type="range" min="0" max="1.5" step="0.05" data-med="vol" value="${m.vol}"></label>` : ""}
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px"><button class="ed-btn sm" data-medfit="full">Full screen</button><button class="ed-btn sm" data-medfit="top">Top</button><button class="ed-btn sm" data-medfit="mid">Middle</button><button class="ed-btn sm" data-medfit="bottom">Bottom</button></div>
          <button class="ed-btn sm" data-e="del" style="width:100%;justify-content:center">${eic("trash")}Remove from the clip</button>`;
        return;
      }
      if (s.k === "cut") {
        el.innerHTML = `<div class="ed-sel"><i>✂</i><div><b>Cut out</b><span>This part is skipped in the clip</span></div></div><button class="ed-btn sm" data-e="del" style="width:100%;justify-content:center">Bring it back</button>`;
        return;
      }
      if (s.k === "range") {
        el.innerHTML = `<div class="ed-sel"><i>⟷</i><div><b>Picked part</b><span>${tc(Math.max(0, toOut(doc, s.a) || 0))} → ${(s.b - s.a).toFixed(1)} s long</span></div></div>
          <button class="ed-btn sm" data-e="del" style="width:100%;justify-content:center">${eic("trash")}Cut this part out <span class="ed-kbd">Del</span></button>
          <div style="display:flex;gap:6px;margin-top:6px"><button class="ed-btn sm" data-e="rngkeep" style="flex:1;justify-content:center" title="The clip becomes just this part">Keep only this</button>
            <button class="ed-btn sm" data-e="rngsplit" style="flex:1;justify-content:center" title="Split at both ends">Split ends</button></div>
          <p class="ed-help" style="margin-top:10px">Drag on the video or captions track to pick a part. Its edges snap to words. <span class="ed-kbd">Esc</span> lets go of it.</p>`;
        return;
      }
      if (s.k === "seg") {
        el.innerHTML = `<div class="ed-sel"><i>▭</i><div><b>Part of the clip</b><span>${(s.b - s.a).toFixed(1)} s</span></div></div><button class="ed-btn sm" data-e="del" style="width:100%;justify-content:center">${eic("trash")}Cut this part out</button>
          <p class="ed-help" style="margin-top:10px">Or drag on the video track to pick exactly the part to cut, then press <span class="ed-kbd">Del</span>.</p>`;
        return;
      }
      if (s.k === "hook") { E.tool = "text"; drawPanel(); }
    }
    const job = jobOf(E.jid), c = job && job.clips.find(x => x.id === E.cid);
    const len = outLen(doc), cuts = cutsMerged(ed.cuts, doc.start, doc.end).length;
    el.innerHTML = `${c && c.score ? `<div style="margin-bottom:12px"><span class="ed-score">${eic("bolt")}Score ${c.score}</span></div>` : ""}
      <div class="ed-stat"><div><b>${tc(len)}</b><span>Length</span></div><div><b>${ed.fx.length}</b><span>Transitions</span></div><div><b>${ed.sfx.length}</b><span>Sounds</span></div><div><b>${cuts}</b><span>Cuts</span></div></div>
      <div class="ed-h">Shortcuts</div>
      <div class="ed-keys">${[["Space", "Play / pause"], ["Drag", "Pick a part on the track"], ["S", "Split at the playhead"], ["Del", "Delete what's selected"], ["J K L", "Back / pause / play"], ["Ctrl Scroll", "Zoom the timeline"], ["T", "Transition here"], ["P", "Punch-in here"],
        ["I O", "Start / end the clip here"], ["← →", "Step a frame (Shift: 1 s)"], ["Ctrl Z", "Undo"], ["Ctrl Shift Z", "Redo"], ["+ −", "Zoom the timeline"], ["Esc", "Back to the clips"]]
        .map(([k, l]) => `<span>${k.split(" ").map(x => `<span class="ed-kbd">${x}</span>`).join("")}</span><span>${l}</span>`).join("")}</div>
      ${c && c.layout_note ? `<p class="ed-help" style="margin-top:14px">${esc(c.layout_note)}</p>` : ""}`;
  }
  function drawState() {
    if (!E) return;
    const el = E.root.querySelector("#edState"); if (!el) return;
    const dirty = JSON.stringify(E.doc) !== E.saved;
    el.className = "ed-state" + (E.exporting ? " busy" : dirty ? " dirty" : "");
    el.querySelector("span").textContent = E.exporting ? "Exporting…" : dirty ? "Changes not exported" : "Up to date";
    const u = E.root.querySelector("#edUndo"), r = E.root.querySelector("#edRedo");
    if (u) u.disabled = E.hi <= 0; if (r) r.disabled = E.hi >= E.hist.length - 1;
    const len = E.root.querySelector("#edLen"); if (len) len.textContent = `Clip ${tc(outLen(E.doc))}`;
  }
  function refresh() { drawPanel(); drawInsp(); drawTimeline(); drawFrame(); drawState(); }

  // ---------------------------------------------------------------- editing
  function select(s) { E.sel = s; E.tsel = []; drawInsp(); drawTimeline(); if (E.tool === "effects") drawPanel(); }
  function split() {
    const t = r3(now()), doc = E.doc;
    if (t <= doc.start + 0.1 || t >= doc.end - 0.1) return toast("Move the playhead inside the clip to split it.");
    doc.splits = [...new Set([...(doc.splits || []), t])].sort((a, b) => a - b);
    commit("Split");
  }
  function addCut(a, b, doc = E.doc, quiet = false) {
    const merged = cutsMerged([...doc.edit.cuts, [a, b]], doc.start, doc.end);
    if ((doc.end - doc.start) - merged.reduce((n, [x, y]) => n + y - x, 0) < 1.5) return quiet ? false : toast("Keep at least a second and a half of the clip.", "bad");
    doc.edit.cuts = merged.map(([x, y]) => [r3(x), r3(y)]);
    return true;
  }
  function del() {
    const s = E.sel, doc = E.doc, ed = doc.edit;
    if (E.tsel.length) {           // words picked in the transcript: cut them out
      const ws = E.data.words.filter(w => E.tsel.includes(w.s)).sort((a, b) => a.s - b.s);
      let ok = false;
      for (const w of ws) ok = addCut(Math.max(doc.start, w.s - 0.02), Math.min(doc.end, w.e + 0.02)) || ok;
      E.tsel = []; if (ok) commit("Cut words"); else refresh(); return;
    }
    if (!s) return;
    if (s.k === "fx") ed.fx.splice(s.i, 1);
    else if (s.k === "sfx") ed.sfx.splice(s.i, 1);
    else if (s.k === "punch") ed.punch.splice(s.i, 1);
    else if (s.k === "media") ed.media.splice(s.i, 1);
    else if (s.k === "cut") { const c = cutsMerged(ed.cuts, doc.start, doc.end); c.splice(s.i, 1); ed.cuts = c; }
    else if (s.k === "seg" || s.k === "range") {
      const a = Math.max(doc.start, s.a), b = Math.min(doc.end, s.b);
      if (b - a < 0.05 || !addCut(a, b)) return;
      toast(`${(b - a).toFixed(1)} s cut out. Ctrl+Z brings it back.`, "ok");
    }
    else if (s.k === "hook") { doc.hook = ""; }
    E.sel = null; commit("Delete");
  }
  function addTransition(type, at) {
    const doc = E.doc, t0 = at != null ? at : now();
    if (t0 < doc.start || t0 > doc.end) return toast("Move the playhead inside the clip first.");
    const near = joins(doc).filter(j => Math.abs(j - t0) <= 1.2).sort((a, b) => Math.abs(a - t0) - Math.abs(b - t0))[0];
    const t = r3(near != null ? near : t0);
    const same = doc.edit.fx.findIndex(f => Math.abs(f.t - t) < 0.05);
    if (same >= 0) { doc.edit.fx[same].type = type; E.sel = {k: "fx", i: same}; }
    else { doc.edit.fx.push({t, type, d: type === "fade" ? 0.5 : type === "flash" ? 0.3 : 0.4, sound: true}); doc.edit.fx.sort((a, b) => a.t - b.t); E.sel = {k: "fx", i: doc.edit.fx.findIndex(f => f.t === t)}; }
    commit("Transition");
    seek(Math.max(doc.start, t - 0.8)); if (!E.playing) play();
  }
  function addSfx(kind) {
    const doc = E.doc, t = r3(clamp(now(), doc.start, doc.end - 0.1));
    doc.edit.sfx.push({t, kind, vol: 0.8}); doc.edit.sfx.sort((a, b) => a.t - b.t);
    E.sel = {k: "sfx", i: doc.edit.sfx.findIndex(s => s.t === t && s.kind === kind)}; hear(kind); commit("Sound");
  }
  function addPunch() {
    const doc = E.doc, t = r3(now());
    if (t < doc.start || t > doc.end - 0.4) return toast("Move the playhead inside the clip first.");
    doc.edit.punch.push({t, z: 1.25, d: 1.2}); doc.edit.punch.sort((a, b) => a.t - b.t);
    E.sel = {k: "punch", i: doc.edit.punch.findIndex(p => p.t === t)}; commit("Punch-in");
  }
  function removeFillers() {
    const doc = E.doc; let n = 0;
    for (const w of E.data.words) if (w.s >= doc.start && w.s < doc.end && FILLER_SAFE.test(w.w.replace(/[^\w.,!?…]/g, "")) && !inCut(doc, (w.s + w.e) / 2)) { if (addCut(w.s - 0.02, w.e + 0.02)) n++; }
    if (n) { commit("Remove fillers"); toast(`${n} filler word${n > 1 ? "s" : ""} cut out.`, "ok"); }
  }
  function hypeApply(doc, data) {    // the Hype Edit recipe: changes doc, says how much it added
    const ed = doc.edit, before = {fx: ed.fx.length, sfx: ed.sfx.length, punch: ed.punch.length};
    // 1. the filler words go, and silences are shortened on export
    for (const w of data.words) if (w.s >= doc.start && w.s < doc.end && FILLER_SAFE.test(w.w.replace(/[^\w.,!?…]/g, "")) && !inCut(doc, (w.s + w.e) / 2)) addCut(w.s - 0.02, w.e + 0.02, doc, true);
    doc.tighten = true;
    // 2. punch in on the hype peaks; the biggest gets a flash, a boom and a riser before it
    const hot = hotMoments(3, data, doc);
    const step = (data.p1 - data.p0) / Math.max(1, data.hype.length);
    const peak = hot.slice().sort((a, b) => (data.hype[Math.round((b - data.p0) / step)] || 0) - (data.hype[Math.round((a - data.p0) / step)] || 0))[0];
    const free = (list, t, gap) => list.every(x => Math.abs(x.t - t) > gap);
    for (const t of hot) if (free(ed.punch, t, 1.5)) ed.punch.push({t: r3(t - 0.1), z: t === peak ? 1.32 : 1.2, d: t === peak ? 1.6 : 1.1});
    if (peak != null) {
      if (free(ed.fx, peak, 0.6)) ed.fx.push({t: r3(peak - 0.05), type: "flash", d: 0.3, sound: true});
      if (peak - doc.start > 2 && free(ed.sfx, peak, 1)) ed.sfx.push({t: r3(peak - 0.05), kind: "riser", vol: 0.55});
    }
    // 3. the cuts get a quick whoosh (a sound on the first few)
    joins(doc).forEach((j, i) => { if (free(ed.fx, j, 0.4)) ed.fx.push({t: r3(j), type: i % 3 === 2 ? "zoom" : "whoosh", d: 0.35, sound: i < 3}); });
    // 4. a punchy look and a popping heading
    if (ed.look === "natural") ed.look = "punchy";
    if (ed.hook_fx === "box") ed.hook_fx = "pop";
    ed.fx.sort((a, b) => a.t - b.t); ed.sfx.sort((a, b) => a.t - b.t); ed.punch.sort((a, b) => a.t - b.t);
    return {fx: ed.fx.length - before.fx, sfx: ed.sfx.length - before.sfx, punch: ed.punch.length - before.punch};
  }
  function hypeEdit() {
    if (!E.data) return;
    const doc = E.doc, n = hypeApply(doc, E.data);
    commit("Hype Edit");
    toast(n.fx + n.sfx + n.punch ? `Hype Edit: ${n.punch} punch-ins, ${n.fx} transitions, ${n.sfx} sounds, filler words cut. Press play.` : "This clip already has its hype edit.", "ok");
    seek(doc.start); if (!E.playing) play();
  }
  function bodyOf(doc, libOk) {      // what Export sends to make the clip
    const ed = doc.edit;
    return {start: r3(doc.start), end: r3(doc.end), hook: doc.hook, title: doc.title, description: doc.description, style: doc.style,
      layout: doc.layout, captions: doc.captions, tighten: doc.tighten, zoom: doc.zoom, watermark: doc.watermark, split: doc.split,
      edit: {cuts: cutsMerged(ed.cuts, doc.start, doc.end), fx: ed.fx.filter(f => f.t >= doc.start && f.t <= doc.end), sfx: ed.sfx.filter(s => s.t >= doc.start - 2 && s.t <= doc.end),
             punch: ed.punch.filter(p => p.t >= doc.start && p.t <= doc.end), look: ed.look, hook_fx: ed.hook_fx, hook_y: ed.hook_y, cap_y: ed.cap_y, hook_dur: ed.hook_dur,
             media: ed.media.filter(m => m.t >= doc.start && m.t <= doc.end && libOk(m.id)), hook_font: libOk(ed.hook_font) ? ed.hook_font : "", cap_font: libOk(ed.cap_font) ? ed.cap_font : "", voice: ed.voice == null ? 1 : ed.voice}};
  }
  /* Hype Edit on a clip without opening it: read its hype, add the edit, make it again. Used by "Hype Edit all"
     on Home and in the Editor page. Says what happened: "done", "same" (already hype-edited) or throws. */
  async function hypeAuto(jid, cid) {
    const clip = (jobOf(jid) || {clips: []}).clips.find(c => c.id === cid);
    if (!clip || clip.status !== "done") throw new Error("This clip isn't ready.");
    const doc = docOf(clip);
    const data = await api(`/api/jobs/${encodeURIComponent(jid)}/clips/${encodeURIComponent(cid)}/editor`);
    const n = hypeApply(doc, data);
    if (!(n.fx + n.sfx + n.punch)) return "same";
    let lib = [];
    if (doc.edit.media.length || doc.edit.hook_font || doc.edit.cap_font) { try { lib = (await api("/api/library")).items || []; } catch {} }
    const body = bodyOf(doc, id => !!id && lib.some(i => i.id === id));
    await send(`/api/jobs/${encodeURIComponent(jid)}/clips/${encodeURIComponent(cid)}/render`, body);
    return "done";
  }

  // ---------------------------------------------------------------- export
  function showMade(c) {          // the export bar: how much of the clip is made, and about how long is left
    if (!E) return;
    const f = c.status === "rendering" ? Math.max(0, Math.min(0.99, c.made || 0)) : 0, pct = Math.round(f * 100);
    const st = E.root.querySelector("#edState span"); if (st) st.textContent = c.status === "rendering" ? `Exporting ${pct}%` : "Waiting to export…";
    const ov = E.ov; if (!ov) return;
    const m = ov.querySelector("#edMeter"), p = ov.querySelector("#edPct"), l = ov.querySelector("#edEtaL"), r = ov.querySelector("#edEtaR");
    if (!m) return;
    m.classList.toggle("wait", c.status !== "rendering");
    m.querySelector("i").style.width = `${Math.max(2, pct)}%`;
    p.innerHTML = `${pct}<small>%</small>`;
    if (c.status === "rendering" && f > 0.02 && !E.expStart) E.expStart = Date.now() - 1;
    const took = E.expStart ? (Date.now() - E.expStart) / 1000 : 0, left = f > 0.04 && took > 2 ? took / f * (1 - f) : 0;
    l.textContent = c.status !== "rendering" ? "Waiting its turn…" : f < 0.02 ? "Starting…" : pct < 95 ? "Making your clip…" : "Almost done…";
    r.textContent = left ? `about ${left < 60 ? Math.max(1, Math.round(left)) + " s" : Math.round(left / 60) + " min"} left` : "";
    const step = c.status !== "rendering" ? 0 : f < 0.35 ? 1 : 2;
    ov.querySelectorAll("#edSteps span").forEach((s, i) => { s.className = i < step ? "done" : i === step ? "on" : ""; });
  }
  async function exportClip() {
    if (E.exporting) return;
    const doc = E.doc, body = bodyOf(doc, id => !!libItem(id));
    if (body.end - body.start < 3) return toast("A clip needs to be at least 3 seconds long.", "bad");
    if (E.playing) play();
    E.exporting = true; drawState();
    const ov = document.createElement("div"); ov.className = "ed-exp"; E.root.append(ov); E.ov = ov;
    ov.innerHTML = `<div class="card"><h3>Exporting your clip</h3><p>1080×1920, with every cut, effect, sound and caption. You can keep editing; it carries on.</p>
      <div class="ed-pct" id="edPct">0<small>%</small></div><div class="ed-meter wait" id="edMeter"><i></i></div>
      <div class="ed-eta"><span id="edEtaL">Waiting its turn…</span><span id="edEtaR"></span></div>
      <div class="ed-steps" id="edSteps"><span class="on">Queued</span><span>Cutting</span><span>Effects &amp; captions</span><span>Done</span></div>
      <div class="row"><button class="ed-btn sm" data-e="hideexp">Keep editing</button></div></div>`;
    E.expAt = Date.now(); E.expStart = 0;
    try {
      await send(`/api/jobs/${E.jid}/clips/${E.cid}/render`, body);
    } catch (e) {
      E.exporting = false; ov.remove(); drawState(); return toast(e.message, "bad");
    }
    E.saved = JSON.stringify(E.doc); E.exported = true;
    try { localStorage.removeItem(E.draftKey); } catch {}
    const jid = E.jid, cid = E.cid;
    let seenBusy = false;
    clearInterval(E.poll);
    E.poll = setInterval(async () => {
      if (!E || E.jid !== jid) return;
      try { await tick(); } catch {}
      const c = (jobOf(jid) || {clips: []}).clips.find(x => x.id === cid); if (!c) return;
      if (c.status === "queued" || c.status === "rendering") { seenBusy = true; showMade(c); return; }
      if (!seenBusy && c.status !== "error") return;
      clearInterval(E.poll); E.exporting = false; drawState();
      if (c.status === "error") { E.ov && (E.ov.innerHTML = `<div class="card"><h3>That didn't work</h3><p>${esc(c.error || "The clip couldn't be made.")}</p><div class="row"><button class="ed-btn sm" data-e="hideexp">Back to the editor</button></div></div>`); return; }
      E.draftKey = `ck-edit:${jid}:${cid}:${c.version || 0}`;
      if (E.ov) E.ov.innerHTML = `<div class="card"><div style="font-size:34px;margin-bottom:6px">✓</div><h3>Your clip is ready</h3><p>${c.layout_note ? esc(c.layout_note) : "Exported in 1080×1920, with your edit."}</p>
        <div class="row"><a class="ed-btn p sm" href="/media/${jid}/${esc(c.file)}?download=1" style="text-decoration:none">${eic("down")}Download</a>
        <button class="ed-btn sm" data-e="gopost">${eic("send")}Post it</button><button class="ed-btn sm" data-e="hideexp">Keep editing</button></div></div>`;
      drawPanel();
    }, 1500);
  }

  // ---------------------------------------------------------------- input
  function onClick(e) {
    const b = e.target.closest("[data-music],[data-e],[data-tool],[data-addtr],[data-settr],[data-addsfx],[data-setsfx],[data-hear],[data-look],[data-hookfx],[data-hookdur],[data-style],[data-layout],[data-tg],[data-go],[data-word],[data-fxsound],[data-split],[data-font],[data-libadd],[data-libdel],[data-medfit]");
    if (!b || !E) return;
    const d = b.dataset, doc = E.doc, ed = doc.edit;
    if (d.word !== undefined && b.closest("#edTrans")) {
      const s = Number(d.word);
      if (e.shiftKey && E.tsel.length) { const ws = E.data.words.map(w => w.s), a = ws.indexOf(E.tsel[0]), z = ws.indexOf(s); E.tsel = ws.slice(Math.min(a, z), Math.max(a, z) + 1); }
      else if (e.ctrlKey || e.metaKey) E.tsel = E.tsel.includes(s) ? E.tsel.filter(x => x !== s) : [...E.tsel, s];
      else { E.tsel = [s]; seek(s); }
      E.sel = null; drawPanel(); drawInsp(); return;
    }
    if (d.word !== undefined) { seek(Number(d.word)); return; }
    if (d.tool) { E.tool = d.tool; E.sel = null; drawPanel(); drawInsp(); placeBoxes(); return; }
    if (d.addtr) return addTransition(d.addtr);
    if (d.settr && E.sel && E.sel.k === "fx") { ed.fx[E.sel.i].type = d.settr; return commit("Transition"); }
    if (d.addsfx) return addSfx(d.addsfx);
    if (d.music) {            // a library sound as music: from the clip's start, under the voice
      const it = libItem(d.music); if (!it) return;
      ed.sfx.push({t: r3(doc.start), kind: "lib:" + d.music, vol: 0.3}); ed.sfx.sort((p, q) => p.t - q.t);
      if ((ed.voice == null ? 1 : ed.voice) > 0.8) ed.voice = 0.8;
      E.sel = {k: "sfx", i: ed.sfx.findIndex(x => x.kind === "lib:" + d.music && x.t === r3(doc.start))};
      toast(`${it.name} plays under the whole clip at 30%. Change its volume on the right.`, "ok"); return commit("Music");
    }
    if (d.setsfx && E.sel && E.sel.k === "sfx") { ed.sfx[E.sel.i].kind = d.setsfx; hear(d.setsfx); return commit("Sound"); }
    if (d.hear) return hear(d.hear);
    if (d.look) { ed.look = d.look; return commit("Look"); }
    if (d.split) { doc.split = d.split; return commit("Facecam size"); }
    if (d.font) { ed[d.font] = d.v || ""; return commit("Font"); }
    if (d.libadd) return addMedia(d.libadd);
    if (d.libdel) { (async () => { if (!await ask("Delete from your library?", "Clips already made keep it. Edits that use it lose it.", "Delete", true)) return; try { await api(`/api/library/${d.libdel}`, {method: "DELETE"}); } catch (err) { return toast(err.message, "bad"); } loadLibrary(); })(); return; }
    if (d.medfit && E.sel && E.sel.k === "media") { const m = ed.media[E.sel.i]; Object.assign(m, {full: {x: .5, y: .5, w: 1}, top: {x: .5, y: .22}, mid: {x: .5, y: .5}, bottom: {x: .5, y: .78}}[d.medfit]); return commit("Place"); }
    if (d.hookfx) { ed.hook_fx = d.hookfx; commit("Text effect"); seek(doc.start); if (!E.playing) play(); return; }
    if (d.hookdur !== undefined) { ed.hook_dur = Number(d.hookdur); return commit("Text length"); }
    if (d.style) { doc.style = d.style; return commit("Caption style"); }
    if (d.layout) { doc.layout = d.layout; return commit("Layout"); }
    if (d.tg) { doc[d.tg] = !doc[d.tg]; return commit(d.tg); }
    if (d.go) { seek(Number(d.go) - 0.6); if (!E.playing) play(); return; }
    if (d.fxsound !== undefined && E.sel && E.sel.k === "fx") { const f = ed.fx[E.sel.i]; f.sound = f.sound === false; return commit("Transition sound"); }
    const a = d.e;
    if (a === "close") return close();
    if (a === "undo") return undo();
    if (a === "redo") return redo();
    if (a === "play") return play();
    if (a === "prev") return seek(now() - 1);
    if (a === "next") return seek(now() + 1);
    if (a === "mute") { if (E.v) { E.v.muted = !E.v.muted; b.textContent = E.v.muted ? "🔇" : "🔊"; } return; }
    if (a === "safe") { E.safe = !E.safe; let s = E.root.querySelector(".ed-safe"); if (E.safe && !s) { s = document.createElement("div"); s.className = "ed-safe"; E.root.querySelector("#edPhone").append(s); } else if (!E.safe && s) s.remove(); E.root.querySelector("#edSafe").style.color = E.safe ? "#C6F432" : ""; if (E.tool === "frame") drawPanel(); return; }
    if (a === "split") return split();
    if (a === "del") return del();
    if (a === "in") { const t = r3(now()); if (doc.end - t < 3) return toast("A clip needs at least 3 seconds."); doc.start = t; return commit("Start"); }
    if (a === "out") { const t = r3(now()); if (t - doc.start < 3) return toast("A clip needs at least 3 seconds."); doc.end = t; return commit("End"); }
    if (a === "addtr") return addTransition("whoosh");
    if (a === "addpunch") return addPunch();
    if (a === "zin") { E.pps = clamp(E.pps * 1.4, 4, 500); return drawTimeline(); }
    if (a === "zout") { E.pps = clamp(E.pps / 1.4, 4, 500); return drawTimeline(); }
    if (a === "zfit") { fitZoom(); drawTimeline(); E.lane.scrollLeft = X(doc.start) - 15; return; }
    if (a === "fillers") return removeFillers();
    if (a === "uncut") { ed.cuts = []; return commit("Bring back"); }
    if (a === "everycut" || a === "allcuts") {
      const type = a === "allcuts" && E.sel && ed.fx[E.sel.i] ? ed.fx[E.sel.i].type : "whoosh", dd = a === "allcuts" && E.sel && ed.fx[E.sel.i] ? ed.fx[E.sel.i].d : 0.35;
      for (const j of joins(doc)) { const i = ed.fx.findIndex(f => Math.abs(f.t - j) < 0.05); if (i >= 0) ed.fx[i].type = type; else ed.fx.push({t: j, type, d: dd, sound: true}); }
      ed.fx.sort((x, y) => x.t - y.t); return commit("Every cut");
    }
    if (a === "resetpos") { ed.hook_y = null; ed.cap_y = null; return commit("Positions"); }
    if (a === "camfix") { D.jid = E.jid; D.cid = E.cid; openCamFix(); const f = document.getElementById("formdlg"); f && f.addEventListener("close", () => setTimeout(reloadData, 400), {once: true}); return; }
    if (a === "hype") return hypeEdit();
    if (a === "rngkeep" && E.sel && E.sel.k === "range") {
      const {a: x, b: y} = E.sel; if (y - x < 3) return toast("A clip needs to be at least 3 seconds long.", "bad");
      doc.start = r3(x); doc.end = r3(y); E.sel = null; seek(doc.start); return commit("Keep only this");
    }
    if (a === "rngsplit" && E.sel && E.sel.k === "range") {
      doc.splits = [...new Set([...(doc.splits || []), ...[E.sel.a, E.sel.b].filter(t => t > doc.start + 0.1 && t < doc.end - 0.1)])].sort((p, q) => p - q);
      E.sel = {k: "seg", a: E.sel.a, b: E.sel.b}; return commit("Split");
    }
    if (a === "export") return exportClip();
    if (a === "upload") { const inp = document.createElement("input"); inp.type = "file"; inp.multiple = true; inp.accept = ".ttf,.otf,.mp3,.wav,.m4a,.aac,.ogg,.opus,.flac,.png,.jpg,.jpeg,.webp,.gif,.mp4,.mov,.webm,.mkv,.m4v"; inp.onchange = () => inp.files.length && upload([...inp.files]); inp.click(); return; }
    if (a === "hideexp") { E.ov && E.ov.remove(); E.ov = null; return; }
    if (a === "gopost") { E.ov && E.ov.remove(); E.ov = null; E.tool = "post"; drawPanel(); return; }
    if (a === "postnow" || a === "postqueue") return post(a === "postnow");
  }
  async function post(now_) {
    const accs = [...E.root.querySelectorAll("[data-pacc]:checked")].map(i => i.value);
    if (!accs.length) return toast("Tick at least one account first.", "bad");
    try { await send("/api/posts", {job: E.jid, clip: E.cid, accounts: accs, now: now_}); toast(now_ ? "Posting now" : "Added to the queue", "ok"); tick(); }
    catch (e) { toast(e.message, "bad"); }
  }
  async function reloadData() {
    if (!E) return;
    try { await tick(); const d = await api(`/api/jobs/${encodeURIComponent(E.jid)}/clips/${encodeURIComponent(E.cid)}/editor`); if (E) { E.data = d; refresh(); } } catch {}
  }
  let inputTimer = null;
  function onInput(e) {
    const t = e.target, doc = E.doc, ed = doc.edit, s = E.sel;
    if (t.dataset.txt) { doc[t.dataset.txt] = t.value; drawFrame(); clearTimeout(inputTimer); inputTimer = setTimeout(() => { commit("Text"); }, 600); return; }
    if (t.dataset.range === "voice") { ed.voice = Number(t.value); const lb = t.parentElement.querySelector("b"); if (lb) lb.textContent = Math.round(ed.voice * 100) + "%"; voiceVol(); return; }
    if (t.dataset.range) { ed[t.dataset.range] = Number(t.value); const lb = t.parentElement.querySelector("b"); if (lb) lb.textContent = t.value; drawFrame(); return; }
    if (t.dataset.fxd !== undefined && s && s.k === "fx") { ed.fx[s.i].d = Number(t.value); t.parentElement.querySelector("b").textContent = Number(t.value).toFixed(2) + " s"; drawFrame(); return; }
    if (t.dataset.sfxvol !== undefined && s && s.k === "sfx") { ed.sfx[s.i].vol = Number(t.value); t.parentElement.querySelector("b").textContent = Math.round(t.value * 100) + "%"; return; }
    if (t.dataset.med && s && s.k === "media") { const m = ed.media[s.i]; m[t.dataset.med] = Number(t.value); const lb = t.parentElement.querySelector("b"); if (lb) lb.textContent = t.dataset.med === "d" ? Number(t.value).toFixed(1) + " s" : Math.round(t.value * 100) + (["w", "vol"].includes(t.dataset.med) ? "%" : ""); drawFrame(); return; }
    if (t.dataset.punz !== undefined && s && s.k === "punch") { ed.punch[s.i].z = Number(t.value); t.parentElement.querySelector("b").textContent = Number(t.value).toFixed(2) + "×"; drawFrame(); return; }
    if (t.dataset.pund !== undefined && s && s.k === "punch") { ed.punch[s.i].d = Number(t.value); t.parentElement.querySelector("b").textContent = Number(t.value).toFixed(1) + " s"; drawFrame(); return; }
  }
  function onChange(e) {
    const t = e.target, doc = E.doc;
    if (t.dataset.num) {
      const v = Number(t.value) + E.data.p0;
      if (!isFinite(v)) return;
      if (t.dataset.num === "start") doc.start = r3(clamp(v, E.data.lo, doc.end - 3));
      else doc.end = r3(clamp(v, doc.start + 3, E.data.p1));
      return commit("Length");
    }
    if (t.dataset.range || t.dataset.med || t.dataset.fxd !== undefined || t.dataset.sfxvol !== undefined || t.dataset.punz !== undefined || t.dataset.pund !== undefined) return commit("Adjust");
  }
  // dragging: the trim handles, transitions, sounds, punch-ins, the playhead, the big text and the captions
  function onDown(e) {
    if (!E || !E.data || e.button !== 0) return;
    const doc = E.doc, ed = doc.edit, t = e.target;
    const laneX = ev => { const r = E.inner.getBoundingClientRect(); return E.data.p0 + (ev.clientX - r.left) / E.pps; };
    const mbx = t.closest("#edMediaBox");
    if (mbx && E.sel && E.sel.k === "media") {
      e.preventDefault(); const r = E.root.querySelector("#edPhone").getBoundingClientRect(), m = ed.media[E.sel.i], k = W / r.width;
      const x0 = e.clientX, y0 = e.clientY, m0 = {...m}, resize = t.tagName === "I";
      const mv = ev => { const dx = (ev.clientX - x0) * k, dy = (ev.clientY - y0) * k;
        if (resize) m.w = clamp(m0.w + dx * 2 / W, 0.05, 1.6); else { m.x = clamp(m0.x + dx / W, -0.3, 1.3); m.y = clamp(m0.y + dy / H, -0.3, 1.3); } drawFrame(); };
      const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); commit("Move"); drawInsp(); };
      addEventListener("pointermove", mv); addEventListener("pointerup", up); return;
    }
    const hb = t.closest("#edHookBox,#edCapBox");
    if (hb) {
      e.preventDefault(); const ph = E.root.querySelector("#edPhone"), r = ph.getBoundingClientRect(), key = hb.id === "edHookBox" ? "hook_y" : "cap_y";
      const y0 = e.clientY, v0 = key === "hook_y" ? hookY() : capY();
      const mv = ev => { ed[key] = Math.round(clamp(v0 + (ev.clientY - y0) * H / r.height, key === "hook_y" ? 120 : 300, 1750)); drawFrame(); };
      const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); commit("Move"); if (key === "hook_y") { E.tool = "text"; drawPanel(); } };
      addEventListener("pointermove", mv); addEventListener("pointerup", up); return;
    }
    if (t.closest("#edTlResize")) {
      e.preventDefault(); const y0 = e.clientY, h0 = E.root.querySelector("#edTl").clientHeight;
      const mv = ev => { E.root.style.setProperty("--tlh", clamp(h0 - (ev.clientY - y0), 170, innerHeight * 0.6) + "px"); };
      const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); drawTimeline(); placeBoxes(); };
      addEventListener("pointermove", mv); addEventListener("pointerup", up); return;
    }
    if (!t.closest("#edInner")) return;
    const handle = t.closest("[data-h]"), fx = t.closest("[data-fx]"), sfx = t.closest("[data-sfx]"), pun = t.closest("[data-punch]"), med = t.closest("[data-media]");
    if (handle || fx || sfx || pun || med) {
      e.preventDefault(); let moved = false; const x0 = e.clientX;
      const kind = handle ? "h" : fx ? "fx" : sfx ? "sfx" : med ? "media" : "punch", i = Number((fx || sfx || pun || med || {dataset: {}}).dataset[kind] ?? -1);
      if (kind !== "h") select({k: kind === "punch" ? "punch" : kind, i});
      const mv = ev => {
        if (Math.abs(ev.clientX - x0) > 3) moved = true; if (!moved) return;
        let tt = clamp(laneX(ev), E.data.lo, E.data.p1);
        if (kind === "h") { if (handle.dataset.h === "start") doc.start = r3(clamp(tt, E.data.lo, doc.end - 3)); else doc.end = r3(clamp(tt, doc.start + 3, E.data.p1)); seek(handle.dataset.h === "start" ? doc.start : doc.end - 0.05); }
        else if (kind === "fx") { const j = joins(doc).find(q => Math.abs(q - tt) * E.pps < 10); ed.fx[i].t = r3(clamp(j != null ? j : tt, doc.start, doc.end)); }
        else if (kind === "sfx") ed.sfx[i].t = r3(clamp(tt, doc.start - 2, doc.end - 0.1));
        else if (kind === "media") ed.media[i].t = r3(clamp(tt, doc.start, doc.end - 0.2));
        else ed.punch[i].t = r3(clamp(tt, doc.start, doc.end - 0.3));
        drawTimeline(); drawFrame();
      };
      const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up);
        if (moved) { if (kind === "fx") ed.fx.sort((a, b) => a.t - b.t); if (kind === "sfx") ed.sfx.sort((a, b) => a.t - b.t); if (kind === "punch") ed.punch.sort((a, b) => a.t - b.t); if (kind === "media") ed.media.sort((a, b) => a.t - b.t); E.sel = null; commit("Move"); }
        else if (kind !== "h") { const at = kind === "fx" ? ed.fx[i].t : kind === "sfx" ? ed.sfx[i].t : kind === "media" ? ed.media[i].t : ed.punch[i].t; seek(Math.max(doc.start, at - 0.6)); } };
      addEventListener("pointermove", mv); addEventListener("pointerup", up); return;
    }
    const cut = t.closest("[data-cut]"), seg = t.closest("[data-tseg]"), hook = t.closest("[data-hook]");
    if (cut) {
      const i = Number(cut.dataset.cut), r = cut.getBoundingClientRect(), edge = e.clientX - r.left < 8 ? 0 : r.right - e.clientX < 8 ? 1 : -1;
      if (edge < 0) { select({k: "cut", i}); return; }
      // an edge of a cut, dragged: the cut gets longer or shorter (the rest of the clip closes up, like a ripple trim)
      e.preventDefault(); const cuts = cutsMerged(ed.cuts, doc.start, doc.end); let moved = false;
      const mv = ev => { moved = true; const tt = laneX(ev), c = cuts[i];
        if (edge === 0) c[0] = r3(clamp(tt, i ? cuts[i - 1][1] + 0.05 : doc.start, c[1] - 0.05)); else c[1] = r3(clamp(tt, c[0] + 0.05, i < cuts.length - 1 ? cuts[i + 1][0] - 0.05 : doc.end));
        ed.cuts = cuts.map(x => [...x]); seek(c[edge]); drawTimeline(); };
      const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up);
        if (!moved) return select({k: "cut", i});
        if ((doc.end - doc.start) - cutsMerged(ed.cuts, doc.start, doc.end).reduce((n, [x, y]) => n + y - x, 0) < 1.5) { toast("Keep at least a second and a half of the clip.", "bad"); undoTo(); return; }
        commit("Trim cut"); };
      addEventListener("pointermove", mv); addEventListener("pointerup", up); return;
    }
    if (hook) { E.sel = {k: "hook"}; E.tool = "text"; drawPanel(); drawTimeline(); return; }
    // on the video or captions track: drag to pick exactly the part to cut (edges snap to words, splits and cuts);
    // a plain click moves the playhead, and picks the part between splits once the clip is split
    const lane = t.closest(".ed-vl,.ed-cl");
    if (lane) {
      e.preventDefault(); E.tsel = [];
      const wasPlaying = E.playing; if (wasPlaying) play();
      const snaps = [doc.start, doc.end, ...(doc.splits || []), ...cutsMerged(ed.cuts, doc.start, doc.end).flat(), ...E.data.words.flatMap(w => [w.s, w.e])];
      const snap = tt => { let best = tt, bd = 7 / E.pps; for (const c of snaps) { const dd = Math.abs(c - tt); if (dd < bd) { bd = dd; best = c; } } return clamp(best, doc.start, doc.end); };
      const t0 = snap(laneX(e)); let moved = false;
      seek(laneX(e));
      const mv = ev => {
        if (!moved && Math.abs(ev.clientX - e.clientX) < 5) return;
        moved = true; const t1 = snap(laneX(ev));
        E.sel = {k: "range", a: r3(Math.min(t0, t1)), b: r3(Math.max(t0, t1))}; seek(t1); drawTimeline();
      };
      const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up);
        if (moved) { if (E.sel && E.sel.k === "range" && E.sel.b - E.sel.a < 0.1) E.sel = null; }
        else if (seg) { const [a, b] = seg.dataset.tseg.split(",").map(Number); E.sel = {k: "seg", a, b}; }
        else E.sel = null;
        drawInsp(); drawTimeline(); if (wasPlaying) play(); };
      addEventListener("pointermove", mv); addEventListener("pointerup", up); return;
    }
    // the playhead: click or drag anywhere else on the timeline
    e.preventDefault(); E.tsel = [];
    if (seg) { const [a, b] = seg.dataset.tseg.split(",").map(Number); E.sel = {k: "seg", a, b}; drawInsp(); }
    else if (E.sel) { E.sel = null; drawInsp(); }
    const wasPlaying = E.playing; if (wasPlaying) play();
    seek(laneX(e)); drawTimeline();
    const mv = ev => { seek(laneX(ev)); };
    const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); if (wasPlaying) play(); };
    addEventListener("pointermove", mv); addEventListener("pointerup", up);
  }
  function onKey(e) {
    if (!E) return;
    const tag = (e.target.tagName || "").toLowerCase(), typing = tag === "input" || tag === "textarea" || tag === "select";
    if (document.querySelector("dialog[open]")) return;        // the facecam box, a question: their keys
    const k = e.key, mod = e.ctrlKey || e.metaKey;
    if (k === "Escape") { if (E.ov) { E.ov.remove(); E.ov = null; } else if (typing) e.target.blur(); else if (E.sel && E.sel.k === "range") { E.sel = null; drawInsp(); drawTimeline(); } else close(); e.preventDefault(); return; }
    if (typing) return;
    if (mod && (k === "z" || k === "Z")) { e.preventDefault(); return e.shiftKey ? redo() : undo(); }
    if (mod && (k === "y" || k === "Y")) { e.preventDefault(); return redo(); }
    if (mod) return;
    const fr = 1 / 30;
    if (k === " ") { e.preventDefault(); return play(); }
    if (k === "s" || k === "S") { e.preventDefault(); return split(); }
    if (k === "Delete" || k === "Backspace") { e.preventDefault(); return del(); }
    if (k === "ArrowLeft") { e.preventDefault(); return seek(now() - (e.shiftKey ? 1 : fr)); }
    if (k === "ArrowRight") { e.preventDefault(); return seek(now() + (e.shiftKey ? 1 : fr)); }
    if (k === "t" || k === "T") { e.preventDefault(); return addTransition("whoosh"); }
    if (k === "p" || k === "P") { e.preventDefault(); return addPunch(); }
    if (k === "i" || k === "I") { e.preventDefault(); return onClick({target: E.root.querySelector('[data-e="in"]'), shiftKey: false}); }
    if (k === "o" || k === "O") { e.preventDefault(); return onClick({target: E.root.querySelector('[data-e="out"]'), shiftKey: false}); }
    if (k === "j" || k === "J") { e.preventDefault(); if (E.playing) play(); return seek(now() - (e.shiftKey ? 5 : 1)); }
    if (k === "k" || k === "K") { e.preventDefault(); if (E.playing) play(); return; }
    if (k === "l" || k === "L") { e.preventDefault(); if (!E.playing) play(); return; }
    if (k === "Home") { e.preventDefault(); return seek(E.doc.start); }
    if (k === "End") { e.preventDefault(); return seek(E.doc.end - 0.05); }
    if (k === "+" || k === "=") { E.pps = clamp(E.pps * 1.4, 4, 500); return drawTimeline(); }
    if (k === "-" || k === "_") { E.pps = clamp(E.pps / 1.4, 4, 500); return drawTimeline(); }
  }
  function onResize() { if (!E) return; drawTimeline(); drawFrame(); }
  function voiceVol() { if (E && E.v) E.v.volume = clamp(E.doc.edit.voice == null ? 1 : E.doc.edit.voice, 0, 1); }     // the stream's own sound, as it will be
  function onWheel(e) {          // Ctrl + scroll zooms the timeline around the pointer; scrolling moves along it
    if (!E || !E.data) return;
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const r = E.lane.getBoundingClientRect(), at = E.data.p0 + (E.lane.scrollLeft + e.clientX - r.left) / E.pps;
      E.pps = clamp(E.pps * (e.deltaY < 0 ? 1.18 : 1 / 1.18), 4, 500); drawTimeline();
      E.lane.scrollLeft = X(at) - (e.clientX - r.left);
    } else if (!e.shiftKey && Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); E.lane.scrollLeft += e.deltaY; }
  }

  window.CKEditor = {open, close, isOpen: () => !!E, hypeAuto,
    // for tests: the edit as it stands
    state: () => E && {doc: deep(E.doc), sel: E.sel, tool: E.tool, t: now(), playing: E.playing, ready: !!(E.v && E.v.videoWidth),
      lib: E.lib.map(i => ({id: i.id, kind: i.kind, type: i.type, ready: !!i.ready})), layout: E.data && geom().lay}};
})();
