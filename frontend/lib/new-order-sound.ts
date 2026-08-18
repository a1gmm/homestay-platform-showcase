/**
 * 新订单提示音：WebAudio 现场合成两声短「叮—叮」。
 * 不加音频文件、不走 CDN。
 *
 * 铁律：本模块**永不抛异常**。声音失败（autoplay 拦截、浏览器不支持）
 * 绝不能影响 toast 弹出——toast 才是主体，声音是锦上添花。
 */

let ctx: AudioContext | null = null;

/** 仅供测试重置模块级单例。 */
export function __resetAudioForTest(): void {
  ctx = null;
}

/**
 * 取 AudioContext（懒创建）。
 * 从 globalThis 取而非 window：浏览器里两者等价，SSR 时 globalThis.AudioContext
 * 本就 undefined（保护同样成立），且这样才可在 node 测试环境里被 stub 到。
 */
function getCtx(): AudioContext | null {
  if (ctx) return ctx;
  const g = globalThis as unknown as {
    AudioContext?: typeof AudioContext;
    webkitAudioContext?: typeof AudioContext;
  };
  const Ctor = g.AudioContext ?? g.webkitAudioContext;
  if (!Ctor) return null;
  ctx = new Ctor();
  return ctx;
}

/** 在 at 时刻响一声。 */
function beep(c: AudioContext, at: number, freq: number): void {
  const osc = c.createOscillator();
  const gain = c.createGain();
  osc.type = "sine";
  osc.frequency.setValueAtTime(freq, at);
  // 快速衰减，听感是「叮」而不是「嘟——」
  gain.gain.setValueAtTime(0.15, at);
  gain.gain.exponentialRampToValueAtTime(0.001, at + 0.18);
  osc.connect(gain);
  gain.connect(c.destination);
  osc.start(at);
  osc.stop(at + 0.2);
}

/**
 * 在首次用户手势时预热音频。**必须在挂载时调一次**，否则前台整段时间都没声音。
 *
 * 为什么必需：登录走的是跨域交接（www 域 → admin 域的 /auth/accept 页），accept
 * 页在 useEffect 里无手势交换凭证后 router.replace 走人。于是前台落在 admin 域这个
 * 文档里时从没点过任何东西，而浏览器的「粘性激活」不跨域名继承 → AudioContext
 * 一直是 suspended → resume() 被拒 → 一声不响。而这功能的设定场景恰恰是
 * 「她开着页面、人在飞书」，也就是她真的不会去点。
 *
 * 监听一次就摘掉，用 once + capture，不拦任何人的点击。
 */
export function primeAudioOnFirstGesture(): () => void {
  if (typeof document === "undefined") return () => {};
  const prime = () => {
    try {
      const c = getCtx();
      if (c && c.state === "suspended") void c.resume().catch(() => {});
    } catch {
      // 预热失败无所谓，playNewOrderDing 自己还会再试一次
    }
  };
  const opts = { once: true, capture: true } as const;
  document.addEventListener("pointerdown", prime, opts);
  document.addEventListener("keydown", prime, opts);
  return () => {
    document.removeEventListener("pointerdown", prime, opts);
    document.removeEventListener("keydown", prime, opts);
  };
}

/** 响「叮—叮」。任何失败都静默吞掉。 */
export function playNewOrderDing(): void {
  try {
    const c = getCtx();
    if (!c) return;
    if (c.state === "suspended") {
      // autoplay 策略：没有用户交互过就会被拦。resume 失败是常态，不是错误。
      void c.resume().catch(() => {});
      // ⚠️ 拒绝在挂起时排音：suspended 状态下 currentTime 冻在 0，此时排的每一声
      // 都堆在 t≈0。等哪次 resume 成功，积攒的十几声会**叠在一起炸响一次**。
      // 宁可这一声不响（toast 还在），也不要日后来一记糊掉的噪音。
      return;
    }
    const t = c.currentTime;
    beep(c, t, 880);
    beep(c, t + 0.22, 1180);
  } catch {
    // 故意吞掉：声音永远不能拖累 toast。
  }
}
