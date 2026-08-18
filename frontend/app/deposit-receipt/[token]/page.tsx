"use client";

/**
 * 押金小票拍照上传页（免登录，靠 URL 里的签名令牌授权）。
 * 前台从飞书入住卡片的「上传押金小票」按钮进来 → 拍照 → 上传 → 存档到该订单。
 * 独立公开页：不依赖登录态/侧边栏，移动端优先，触控目标 ≥44px。
 */
import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";

const API = "/api/v1/deposit-receipt";

type Meta = {
  order_id: string;
  guest_name: string;
  room_label: string | null;
  existing_count: number;
};

const COLORS = {
  bg: "#FBF8F1",
  ink: "#2B2721",
  sub: "#7A736A",
  brand: "#2B7A6F",
  line: "#E7E1D6",
  danger: "#B0402F",
};

export default function DepositReceiptPage() {
  const { token } = useParams<{ token: string }>();

  const [meta, setMeta] = useState<Meta | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [note, setNote] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  const [doneCount, setDoneCount] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`${API}/${token}/meta`);
        if (!res.ok) {
          const b = await res.json().catch(() => ({}));
          if (alive) setLoadErr(b.detail || "链接无效或已过期");
          return;
        }
        if (alive) setMeta(await res.json());
      } catch {
        if (alive) setLoadErr("网络异常，请重试");
      }
    })();
    return () => {
      alive = false;
    };
  }, [token]);

  // 上传中拦一下关页面/后退：照片传一半关掉就白传了，浏览器弹窗提醒一下。
  useEffect(() => {
    if (!uploading) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [uploading]);

  function pickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] || null;
    setUploadErr(null);
    setFile(f);
    setPreview(f ? URL.createObjectURL(f) : null);
  }

  async function upload() {
    const trimmed = note.trim();
    if (!file && !trimmed) return; // 图片和备注至少一个
    setUploading(true);
    setUploadErr(null);
    // 超时保护：跨海上传偶发很慢，45s 没完就中止并提示，绝不无限转。
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 45000);
    try {
      const fd = new FormData();
      if (file) {
        // 上传前压小手机原图：小票清晰可扫即可，体积从几 MB 降到几百 KB，传得快很多；
        // 顺带把 iPhone HEIC 转 JPEG。
        const blob = await compressImage(file);
        fd.append("file", blob, "receipt.jpg");
      }
      if (trimmed) fd.append("note", trimmed);
      const res = await fetch(`${API}/${token}`, { method: "POST", body: fd, signal: ctrl.signal });
      const b = await res.json().catch(() => ({}));
      if (!res.ok) {
        setUploadErr(b.detail || "上传失败，请重试");
        return;
      }
      setDoneCount(b.count ?? (meta?.existing_count ?? 0) + 1);
      setFile(null);
      setNote("");
      setPreview(null);
      if (inputRef.current) inputRef.current.value = "";
      setMeta((m) => (m ? { ...m, existing_count: b.count ?? m.existing_count + 1 } : m));
    } catch (e) {
      setUploadErr(
        (e as Error)?.name === "AbortError" ? "上传超时，请重试（可改用手打备注）" : "网络异常，请重试"
      );
    } finally {
      clearTimeout(timer);
      setUploading(false);
    }
  }

  function reset() {
    setDoneCount(null);
    setFile(null);
    setNote("");
    setPreview(null);
    setUploadErr(null);
  }

  return (
    <div style={{ minHeight: "100vh", background: COLORS.bg, color: COLORS.ink }}>
      <div style={{ maxWidth: 520, margin: "0 auto", padding: "24px 18px 48px" }}>
        <div style={{ fontSize: 22, fontWeight: 600, marginBottom: 4 }}>📸 押金小票存档</div>
        <div style={{ fontSize: 14, color: COLORS.sub, marginBottom: 20 }}>
          拍下 POS 小票，或手打一条备注 —— 二选一即可，退房退押金时用
        </div>

        {loadErr && (
          <div style={box(COLORS.danger)}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>无法打开</div>
            <div style={{ fontSize: 14 }}>{loadErr}</div>
            <div style={{ fontSize: 13, color: COLORS.sub, marginTop: 10 }}>
              链接可能已过期，请联系前台重新发起。
            </div>
          </div>
        )}

        {!loadErr && !meta && <div style={{ color: COLORS.sub }}>加载中…</div>}

        {meta && (
          <>
            <div style={box(COLORS.line)}>
              <Row label="订单" value={meta.order_id} />
              <Row label="客人" value={meta.guest_name || "—"} />
              <Row label="房间" value={meta.room_label || "—"} />
              {meta.existing_count > 0 && (
                <Row label="已存档" value={`${meta.existing_count} 条`} />
              )}
            </div>

            {doneCount !== null ? (
              <div style={{ ...box(COLORS.brand), textAlign: "center" }}>
                <div style={{ fontSize: 40, marginBottom: 6 }}>✅</div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>已存档</div>
                <div style={{ fontSize: 14, color: COLORS.sub }}>
                  已挂到订单（共 {doneCount} 条），可以关闭页面了。
                </div>
                <button style={btnGhost} onClick={reset}>
                  再存一条
                </button>
              </div>
            ) : (
              <>
                <input
                  ref={inputRef}
                  type="file"
                  accept="image/*"
                  capture="environment"
                  onChange={pickFile}
                  style={{ display: "none" }}
                  id="receipt-input"
                />

                {preview ? (
                  <div style={{ marginTop: 8 }}>
                    <img
                      src={preview}
                      alt="小票预览"
                      style={{
                        width: "100%",
                        borderRadius: 12,
                        border: `1px solid ${COLORS.line}`,
                        maxHeight: "50vh",
                        objectFit: "contain",
                        background: "#fff",
                      }}
                    />
                    <label htmlFor="receipt-input" style={{ ...btnGhost, display: "block", textAlign: "center" }}>
                      重拍 / 换一张
                    </label>
                  </div>
                ) : (
                  <label htmlFor="receipt-input" style={btnPrimaryLike}>
                    拍照 / 选择小票照片
                  </label>
                )}

                <div style={{ marginTop: 16 }}>
                  <div style={{ fontSize: 13, color: COLORS.sub, marginBottom: 6 }}>
                    备注（没拍照可只填这里）
                  </div>
                  <textarea
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    maxLength={200}
                    placeholder="例：押金 ¥100，微信预收，流水号 000460"
                    style={{
                      width: "100%",
                      minHeight: 72,
                      boxSizing: "border-box",
                      padding: 12,
                      fontSize: 16,
                      borderRadius: 12,
                      border: `1px solid ${COLORS.line}`,
                      background: "#fff",
                      color: COLORS.ink,
                      resize: "vertical",
                    }}
                  />
                </div>

                {uploadErr && (
                  <div style={{ color: COLORS.danger, fontSize: 14, marginTop: 12 }}>{uploadErr}</div>
                )}

                <button
                  style={{ ...btnPrimary, opacity: (!file && !note.trim()) || uploading ? 0.5 : 1 }}
                  disabled={(!file && !note.trim()) || uploading}
                  onClick={upload}
                >
                  {uploading ? "上传中…" : "上传存档"}
                </button>
                {uploading && (
                  <div style={{ color: COLORS.sub, fontSize: 13, textAlign: "center", marginTop: 10 }}>
                    上传中，请勿关闭页面
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// 客户端压图：最长边缩到 1600px、JPEG q0.72。小票 + 二维码在这个分辨率下清晰可扫，
// 但体积从几 MB 降到几百 KB，上传快很多。任何解码/压缩失败一律退回原图，绝不因压图传不了。
async function compressImage(file: File): Promise<Blob> {
  const MAX = 1600;
  const QUALITY = 0.72;
  try {
    const img = await loadImage(file);
    const iw = (img as HTMLImageElement).naturalWidth || (img as ImageBitmap).width;
    const ih = (img as HTMLImageElement).naturalHeight || (img as ImageBitmap).height;
    if (!iw || !ih) return file;
    const scale = Math.min(1, MAX / Math.max(iw, ih));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(iw * scale);
    canvas.height = Math.round(ih * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) return file;
    ctx.drawImage(img as CanvasImageSource, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", QUALITY)
    );
    if (!blob || blob.size >= file.size) return file; // 没变小就用原图
    return blob;
  } catch {
    return file;
  }
}

async function loadImage(file: File): Promise<ImageBitmap | HTMLImageElement> {
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(file);
    } catch {
      /* HEIC 等个别格式 createImageBitmap 失败 → 退回 <img> 解码 */
    }
  }
  return await new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = URL.createObjectURL(file);
  });
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", fontSize: 15 }}>
      <span style={{ color: COLORS.sub }}>{label}</span>
      <span style={{ fontWeight: 500, textAlign: "right", marginLeft: 16 }}>{value}</span>
    </div>
  );
}

function box(border: string): React.CSSProperties {
  return {
    background: "#fff",
    border: `1px solid ${border}`,
    borderRadius: 14,
    padding: 16,
    marginBottom: 16,
  };
}

const btnBase: React.CSSProperties = {
  display: "block",
  width: "100%",
  minHeight: 52,
  lineHeight: "52px",
  borderRadius: 12,
  fontSize: 16,
  fontWeight: 600,
  textAlign: "center",
  cursor: "pointer",
  marginTop: 16,
  border: "none",
};

const btnPrimary: React.CSSProperties = { ...btnBase, background: COLORS.brand, color: "#fff" };
const btnPrimaryLike: React.CSSProperties = { ...btnBase, background: COLORS.brand, color: "#fff", boxSizing: "border-box" };
const btnGhost: React.CSSProperties = {
  ...btnBase,
  background: "transparent",
  color: COLORS.brand,
  border: `1px solid ${COLORS.brand}`,
  minHeight: 44,
  lineHeight: "44px",
  fontSize: 15,
  boxSizing: "border-box",
};
