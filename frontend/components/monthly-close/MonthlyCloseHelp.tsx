"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Drawer } from "antd";
import { BookOpen, MessageSquare } from "lucide-react";
import { monthlyCloseSupportApi } from "@/lib/api";
import { feedbackSteps, feedbackStatusLabels, type FeedbackStep, type FeedbackStatus, type MonthlyCloseFeedback, type MonthlyCloseFeedbackInput } from "@/lib/monthly-close-support";
import styles from "./MonthlyCloseHelp.module.css";

function errorMessage(error: unknown, fallback: string) {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : fallback;
}
function readImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("截图读取失败，请重新选择"));
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.readAsDataURL(file);
  });
}

// Parent keys this component by account, login session and month. Never share drafts across those scopes.
export function MonthlyCloseHelp({ month, privacyMode = false }: { month: string; privacyMode?: boolean }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState("guide");
  const [page, setPage] = useState(1);
  const [pageUrl, setPageUrl] = useState("");
  const pageCache = useRef<Record<number, string>>({});
  const pageSequence = useRef(0);
  const [guideUrl, setGuideUrl] = useState("");
  const [guideError, setGuideError] = useState("");
  const [guideLoading, setGuideLoading] = useState(false);
  const [step, setStep] = useState<FeedbackStep>("other");
  const [description, setDescription] = useState("");
  const [expected, setExpected] = useState("");
  const [executed, setExecuted] = useState<MonthlyCloseFeedbackInput["execution_clicked"]>("unknown");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState("");
  const [busy, setBusy] = useState(false);
  const [records, setRecords] = useState<MonthlyCloseFeedback[]>([]);
  const [total, setTotal] = useState(0);
  const [listError, setListError] = useState("");
  const [listLoading, setListLoading] = useState(false);
  const lifetime = useRef<AbortController | null>(null);
  const objectUrls = useRef<string[]>([]);
  const submitting = useRef(false);
  const retry = useRef<{ fingerprint: string; id: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const listSequence = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => { controller.abort(); objectUrls.current.forEach((url) => URL.revokeObjectURL(url)); objectUrls.current = []; pageCache.current = {}; };
  }, []);
  const loadGuide = useCallback(async (nextPage = 1) => {
    const sequence = ++pageSequence.current;
    const signal = lifetime.current?.signal;
    setGuideLoading(true); setGuideError("");
    try {
      let url = pageCache.current[nextPage];
      if (!url) {
        const { data } = await monthlyCloseSupportApi.guidePage(nextPage, signal);
        if (signal?.aborted || sequence !== pageSequence.current) return;
        url = URL.createObjectURL(data); objectUrls.current.push(url); pageCache.current[nextPage] = url;
      }
      setPage(nextPage); setPageUrl(url);
      if (!guideUrl) {
        const { data } = await monthlyCloseSupportApi.guide(signal);
        if (signal?.aborted || sequence !== pageSequence.current) return;
        const pdfUrl = URL.createObjectURL(data); objectUrls.current.push(pdfUrl); setGuideUrl(pdfUrl);
      }
    } catch { if (!signal?.aborted) setGuideError("教程暂时没能加载，请重试。也可以先提交问题反馈。"); }
    finally { if (!signal?.aborted) setGuideLoading(false); }
  }, [guideUrl]);
  const loadRecords = useCallback(async (offset = 0) => {
    const signal = lifetime.current?.signal;
    const sequence = ++listSequence.current;
    setListLoading(true); setListError("");
    try {
      const { data } = await monthlyCloseSupportApi.list(month, offset, signal);
      if (signal?.aborted || sequence !== listSequence.current) return;
      setRecords((previous) => offset ? [...previous, ...data.items.filter((item) => !previous.some((old) => old.feedback_id === item.feedback_id))] : data.items);
      setTotal(data.total);
    } catch { if (!signal?.aborted && sequence === listSequence.current) setListError("反馈记录加载失败，点击刷新重试。"); }
    finally { if (!signal?.aborted && sequence === listSequence.current) setListLoading(false); }
  }, [month]);
  const show = (next: string) => {
    setOpen(true); setTab(next);
    if (next === "guide" && !pageUrl && !guideLoading) void loadGuide();
    if (next === "records") void loadRecords();
  };
  const submit = async () => {
    if (submitting.current || privacyMode) return;
    if (description.trim().length < 5) { setError("请用至少 5 个字说明遇到的问题。"); return; }
    submitting.current = true; setBusy(true); setError(""); setReceipt("");
    const signal = lifetime.current?.signal;
    try {
      const payload = { billing_month: month, step, description: description.trim(), expected: expected.trim(), execution_clicked: executed, ...(file ? { screenshot_base64: await readImage(file) } : {}) };
      if (signal?.aborted) return;
      const fingerprint = JSON.stringify(payload);
      if (retry.current?.fingerprint !== fingerprint) retry.current = { fingerprint, id: crypto.randomUUID() };
      const { data } = await monthlyCloseSupportApi.create({ ...payload, request_id: retry.current.id }, signal);
      if (signal?.aborted) return;
      setReceipt(`反馈已保存 · ${data.feedback_id}。管理员可在“反馈记录”查看和处理。`);
      setDescription(""); setExpected(""); setFile(null); setExecuted("unknown"); retry.current = null;
      if (fileInput.current) fileInput.current.value = "";
      setTab("records"); void loadRecords();
    } catch (failure) {
      if (!signal?.aborted) setError(errorMessage(failure, "暂未确认提交成功，填写内容已保留。请重试，同一份内容不会重复提交。"));
    } finally { submitting.current = false; if (!signal?.aborted) setBusy(false); }
  };

  return <>
    <Button icon={<BookOpen size={16} />} style={{ minHeight: 44 }} disabled={privacyMode} onClick={() => show("guide")}>图文教程</Button>
    <Button icon={<MessageSquare size={16} />} style={{ minHeight: 44 }} disabled={privacyMode} onClick={() => show("feedback")}>反馈问题</Button>
    <Drawer title="月结助理 · 使用帮助" open={open && !privacyMode} onClose={() => setOpen(false)} width={960}>
      <div className={styles.root}>
        <nav className={styles.tabs} aria-label="使用帮助栏目">
          {[["guide", "图文教程"], ["feedback", "反馈问题"], ["records", "反馈记录"]].map(([key, label]) => <Button key={key} aria-pressed={tab === key} type={tab === key ? "primary" : "default"} disabled={privacyMode && key !== "guide"} onClick={() => show(key)}>{label}</Button>)}
        </nav>
        {tab === "guide" && <section>
          <h2 className={styles.heading}>照着做一遍，卡住就反馈</h2>
          <p className={styles.intro}>7 页图文手册 · 2026年10月19日业主对账版。按“交资料 → 回答待核问题 → 查看业主账单”试用；含问题卡片、方案确认、下载账单、完成本月业主对账和平台反馈步骤，最后一页可逐项打勾。截图中的 9 月数据和演示金额只用于说明。</p>
          {guideLoading && <p role="status">正在打开教程…</p>}
          {guideError && <div className={styles.error} role="alert">{guideError} <Button onClick={() => void loadGuide()}>重新加载教程</Button></div>}
          {guideUrl && <>
            <div className={styles.actions}>
              <Button href={guideUrl} target="_blank" rel="noopener noreferrer">新窗口查看</Button>
              <Button href={guideUrl} download="月结助理_三步流程图文试用手册.pdf">下载 PDF</Button>
              <Button onClick={() => show("feedback")} disabled={privacyMode}>有问题，直接反馈</Button>
            </div>
            <p className={styles.meta}>下方可直接翻页阅读；需要转发或打印时，请下载 PDF。</p>
          </>}
          {pageUrl && <>
            <div className={styles.actions} style={{ marginTop: 20 }}>
              <Button disabled={page === 1 || guideLoading} onClick={() => void loadGuide(page - 1)}>上一页</Button>
              <label>跳到第 <select aria-label="教程页码" value={page} disabled={guideLoading} onChange={(event) => void loadGuide(Number(event.target.value))} style={{ minHeight: 44, background: "var(--shell)", border: "1px solid var(--linen)", borderRadius: 8, padding: "0 8px", font: "inherit" }}>{[1,2,3,4,5,6,7].map((value) => <option key={value} value={value}>{value}</option>)}</select> 页 / 共 7 页</label>
              <Button disabled={page === 7 || guideLoading} onClick={() => void loadGuide(page + 1)}>下一页</Button>
            </div>
            {/* Authenticated raster preview also works in browsers without a PDF viewer. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img className={styles.guidePage} src={pageUrl} alt={`月结助理图文试用手册，第 ${page} 页，共 7 页。可下载 PDF 阅读文字。`} />
          </>}
        </section>}
        {!privacyMode && tab === "feedback" && <section>
          <h2 className={styles.heading}>告诉我们，你卡在哪里</h2>
          <p className={styles.intro}>当前月份：{month}。反馈保存在平台内供管理员处理，不会自动修改账目。请先遮住截图中的密码、手机号等无关信息。</p>
          <form className={styles.form} onSubmit={(event) => { event.preventDefault(); void submit(); }}>
            <label className={styles.field}>卡住的步骤<select value={step} disabled={busy} onChange={(event) => setStep(event.target.value as FeedbackStep)}>{feedbackSteps.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
            <label className={styles.field}>问题说明（必填）<textarea rows={4} maxLength={3000} disabled={busy} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="例如：上传两份保洁表后，一直显示识别中，刷新后仍一样。" /></label>
            <label className={styles.field}>你原本希望看到什么（选填）<textarea rows={2} maxLength={1500} disabled={busy} value={expected} onChange={(event) => setExpected(event.target.value)} placeholder="例如：看到两份表的次数、金额，以及需要核对的差异。" /></label>
            <label className={styles.field}>是否点过“确认执行”<select value={executed} disabled={busy} onChange={(event) => setExecuted(event.target.value as typeof executed)}><option value="unknown">不确定 / 没有这个按钮</option><option value="no">还没有点</option><option value="yes">已经点过</option></select></label>
            <label className={styles.field}>问题截图（选填）<input aria-label="问题截图（选填）" ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={(event) => {
              const selected = event.target.files?.[0]; setError("");
              if (selected && (selected.size > 3 * 1024 * 1024 || !["image/png", "image/jpeg", "image/webp"].includes(selected.type))) { setError("请选择 3MB 以内的 PNG、JPG 或 WebP 截图。"); event.target.value = ""; setFile(null); return; }
              setFile(selected ?? null);
            }} /><small>一张即可，最多 3MB。请截到按钮文字和错误提示。</small></label>
            {file && <Button disabled={busy} onClick={() => { setFile(null); if (fileInput.current) fileInput.current.value = ""; }}>移除截图</Button>}
            {error && <div className={styles.error} role="alert">{error}</div>}
            <div><Button aria-label="提交问题" type="primary" htmlType="submit" loading={busy}>提交问题</Button></div>
          </form>
        </section>}
        {!privacyMode && tab === "records" && <section>
          <h2 className={styles.heading}>{month} 的反馈记录</h2>
          <p className={styles.intro}>本月所有管理员提交的问题都在这里。处理状态只代表反馈进展；请按处理说明复试，账目仍需单独核对。</p>
          {receipt && <p role="status" className={styles.reply}>{receipt}</p>}
          <Button loading={listLoading} onClick={() => void loadRecords()}>刷新反馈记录</Button>
          {listError && <p role="alert" className={styles.error}>{listError}</p>}
          {!listLoading && !listError && records.length === 0 && <p>本月还没有反馈。</p>}
          {records.map((record) => <FeedbackRecord key={`${record.feedback_id}:${record.revision}`} record={record} getSignal={() => lifetime.current?.signal} registerUrl={(url) => objectUrls.current.push(url)} onUpdated={() => void loadRecords()} />)}
          {records.length < total && <Button loading={listLoading} onClick={() => void loadRecords(records.length)}>加载更多反馈</Button>}
        </section>}
      </div>
    </Drawer>
  </>;
}

function FeedbackRecord({ record, getSignal, registerUrl, onUpdated }: { record: MonthlyCloseFeedback; getSignal: () => AbortSignal | undefined; registerUrl: (url: string) => void; onUpdated: () => void }) {
  const [editing, setEditing] = useState(false);
  const [reply, setReply] = useState(record.reply);
  const [status, setStatus] = useState(record.status);
  const [image, setImage] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const pending = useRef(false);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const save = async () => {
    const signal = getSignal();
    if (pending.current) return;
    if (reply.trim().length < 2) { setError("请填写处理说明，方便对方按说明复试。"); return; }
    pending.current = true; setLoading(true); setError("");
    try {
      await monthlyCloseSupportApi.update(record.feedback_id, { revision: record.revision, status, reply: reply.trim() }, signal);
      if (!signal?.aborted && live.current) { setEditing(false); onUpdated(); }
    } catch (failure) { if (!signal?.aborted && live.current) setError(errorMessage(failure, "保存失败，内容已保留，请重试。")); }
    finally { pending.current = false; if (!signal?.aborted && live.current) setLoading(false); }
  };
  return <article className={styles.record}>
    <div className={styles.status}>● {feedbackStatusLabels[record.status]}</div>
    <p>{feedbackSteps.find(([value]) => value === record.step)?.[1]} · {new Date(record.created_at).toLocaleString("zh-CN")}</p>
    <p>{record.description}</p>
    {record.expected && <p>希望看到：{record.expected}</p>}
    <p className={styles.meta}>确认执行：{record.execution_clicked === "yes" ? "已经点过" : record.execution_clicked === "no" ? "还没有点" : "不确定"} · {record.feedback_id}</p>
    {record.reply && <div className={styles.reply}>处理说明：{record.reply}</div>}
    <div className={styles.actions}>
      {record.screenshot_mime && !image && <Button onClick={async () => {
        const signal = getSignal();
        try { const { data } = await monthlyCloseSupportApi.screenshot(record.feedback_id, signal); if (!signal?.aborted && live.current) { const url = URL.createObjectURL(data); registerUrl(url); setImage(url); } }
        catch { if (!signal?.aborted && live.current) setError("截图加载失败，请重试。"); }
      }}>查看问题截图</Button>}
      <Button onClick={() => setEditing(!editing)}>{editing ? "收起处理表单" : "填写处理进展"}</Button>
    </div>
    {/* Authenticated object URL; no public image endpoint or Next image proxy. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {image && <img className={styles.screenshot} src={image} alt="本条反馈的问题截图" />}
    {editing && <div className={styles.form} style={{ marginTop: 16 }}>
      <label className={styles.field}>处理状态<select disabled={loading} value={status} onChange={(event) => setStatus(event.target.value as FeedbackStatus)}>{Object.entries(feedbackStatusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      <label className={styles.field}>处理说明<textarea rows={3} maxLength={2000} disabled={loading} value={reply} onChange={(event) => setReply(event.target.value)} placeholder="说明已检查什么、如何操作，以及需要对方重新试哪一步。" /></label>
      <div><Button loading={loading} onClick={() => void save()}>保存处理进展</Button></div>
    </div>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </article>;
}
