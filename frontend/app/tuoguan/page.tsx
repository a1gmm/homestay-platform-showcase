import type { Metadata } from "next";
import Image from "next/image";
import LeadForm from "./LeadForm";
import { TUOGUAN } from "./config";

export const metadata: Metadata = {
  metadataBase: new URL("https://showcase.example.invalid"),
  // 主域/admin 域访问 /tuoguan 路径不拦截（设计决策），canonical 收敛到子域名
  alternates: { canonical: "https://showcase.example.invalid" },
  title: "观海居·业主托管合作 | 您的海景房，空着也在花钱",
  description:
    "青岛灵山湾闲置房屋托管：托管收益约为长租两倍，业主可自住，五星级标准维护，经营数据全透明。观海居团队来自希尔顿、爱彼迎、携程。",
  openGraph: {
    title: "观海居·业主托管合作",
    description: "闲置海景房交给观海居，收益翻倍，随时自住。",
    images: ["/tuoguan/hero.jpg"],
  },
};

const NAVY = TUOGUAN.navy;
const ORANGE = TUOGUAN.orange;

const sectionStyle: React.CSSProperties = {
  maxWidth: 640,
  margin: "0 auto",
  padding: "40px 20px",
};

const h2Style: React.CSSProperties = {
  fontSize: 24,
  fontWeight: 700,
  color: NAVY,
  textAlign: "center",
  margin: "0 0 24px",
};

function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        display: "inline-block",
        background: ORANGE,
        color: "#fff",
        fontSize: 13,
        fontWeight: 600,
        padding: "2px 10px",
        borderRadius: 4,
        marginBottom: 8,
      }}
    >
      {children}
    </span>
  );
}

function Card(props: { title: string; body: string }) {
  return (
    <div
      style={{
        background: "#fff",
        borderRadius: 12,
        padding: 20,
        boxShadow: "0 2px 8px rgba(31,75,122,0.08)",
      }}
    >
      <p style={{ fontSize: 17, fontWeight: 700, color: NAVY, margin: "0 0 6px" }}>
        {props.title}
      </p>
      <p style={{ fontSize: 14, lineHeight: 1.7, color: "#555", margin: 0 }}>{props.body}</p>
    </div>
  );
}

export default function TuoguanPage() {
  return (
    <main style={{ background: "#F7F9FC", paddingBottom: 76 /* 给悬浮底栏让位 */ }}>
      {/* 1. Hero */}
      <section style={{ position: "relative", color: "#fff", background: NAVY }}>
        <Image
          src="/tuoguan/hero.jpg"
          alt="灵山湾海景房实拍"
          width={1200}
          height={675}
          priority
          style={{ width: "100%", height: "auto", display: "block", opacity: 0.45 }}
        />
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
            alignItems: "center",
            textAlign: "center",
            padding: 20,
          }}
        >
          <p style={{ fontSize: 14, letterSpacing: 4, margin: "0 0 8px", color: ORANGE }}>
            {TUOGUAN.brand} GUAN HAI JU
          </p>
          <h1 style={{ fontSize: 28, fontWeight: 800, margin: "0 0 10px", lineHeight: 1.4 }}>
            您的海景房，空着也在花钱
          </h1>
          <p style={{ fontSize: 15, margin: "0 0 20px", opacity: 0.9 }}>
            点亮家庭度假生活 · 重新定义旅游地产主人身份
          </p>
          <a
            href="#lead-form"
            style={{
              background: ORANGE,
              color: "#fff",
              fontSize: 16,
              fontWeight: 600,
              padding: "13px 32px",
              borderRadius: 999,
              textDecoration: "none",
            }}
          >
            免费评估我的房子
          </a>
        </div>
      </section>

      {/* 2. 痛点 */}
      <section style={sectionStyle}>
        <h2 style={h2Style}>闲置的房子，正在悄悄贬值</h2>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Card
            title="房屋损坏"
            body="长期闲置的房屋会出现家具腐蚀、墙面发霉、室内异味等加速损坏情况。"
          />
          <Card
            title="收益低"
            body="长租不仅收益低，每年频繁换租户费时费力，而且不能保证房屋状态，不能满足自己和家人自住需求。"
          />
          <Card
            title="维护费心"
            body="每年实际入住一个月，却要花费时间、精力和费用维护房屋设施设备，影响度假心情。"
          />
        </div>
      </section>

      {/* 3. 托管 vs 长租 */}
      <section style={{ ...sectionStyle, background: "#fff", maxWidth: "none" }}>
        <div style={{ maxWidth: 640, margin: "0 auto" }}>
          <h2 style={h2Style}>托管，比长租多得多</h2>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {[
              ["业主自住", "托管不影响自己和家人随时入住，长租一般情况下不能自住。"],
              ["收益回报", "托管收益相当于长租收益的两倍左右。"],
              ["房屋维护", "像高端酒店一样经营，做到五星级的保洁维护。"],
              ["房屋保值", "长期维护保持好状态，出售时交易更快、价格更高。"],
            ].map(([t, b], i) => (
              <div
                key={t}
                style={{
                  border: `1px solid ${i % 2 ? `${ORANGE}33` : `${NAVY}22`}`,
                  borderTop: `3px solid ${i % 2 ? ORANGE : NAVY}`,
                  borderRadius: 10,
                  padding: 16,
                }}
              >
                <p style={{ fontSize: 16, fontWeight: 700, color: NAVY, margin: "0 0 6px" }}>{t}</p>
                <p style={{ fontSize: 14, lineHeight: 1.7, color: "#555", margin: 0 }}>{b}</p>
              </div>
            ))}
          </div>
          <p style={{ fontSize: 13, color: "#888", textAlign: "center", marginTop: 16 }}>
            软装品质提升后：减少空置期 · 提高租金收益 20%-50% · 出售成交价更高
          </p>
        </div>
      </section>

      {/* 4. 服务内容 */}
      <section style={sectionStyle}>
        <h2 style={h2Style}>托管为业主提供什么</h2>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Card title="VIP 接待" body="业主入住期间提供管家、保洁等 VIP 接待服务。" />
          <Card title="深度护理" body="定期对房屋做家具器皿养护、除湿、去霉等深度房屋护理。" />
          <Card title="一站代办" body="房屋买卖代办，一站式服务。" />
          <Card
            title="出行代订"
            body="景区景点、演艺演出门票代订代付；游艇、潜水、跳伞等户外项目预订。"
          />
        </div>
        <div
          style={{
            marginTop: 16,
            background: NAVY,
            color: "#fff",
            borderRadius: 12,
            padding: 20,
          }}
        >
          <Tag>经营数据全透明</Tag>
          <p style={{ fontSize: 14, lineHeight: 1.8, margin: 0 }}>
            入户门口安装品牌无线摄像头，业主手机 24 小时随时查看；店长后台管理系统共享给业主，
            入住价格、渠道来源一目了然，经营业绩完全公开透明。
          </p>
        </div>
      </section>

      {/* 5. 品牌与团队 */}
      <section style={{ ...sectionStyle, background: "#fff", maxWidth: "none" }}>
        <div style={{ maxWidth: 640, margin: "0 auto", textAlign: "center" }}>
          <h2 style={h2Style}>为什么选观海居</h2>
          <p style={{ fontSize: 14, lineHeight: 1.9, color: "#555", margin: "0 0 16px" }}>
            观海居以「点亮家庭度假生活」为使命，床品、洗漱用品、保洁对标五星级酒店。
            团队人员来自<strong>希尔顿、爱彼迎、携程</strong>等星级酒店管理公司和知名服务企业，
            协力打造民宿名片企业。
          </p>
          <p style={{ fontSize: 13, color: "#888", margin: 0 }}>
            专属会员制度 · 抖音/小红书内容获客 · 2000+ 团队推荐 · OTA 全渠道分销
          </p>
        </div>
      </section>

      {/* 6. 案例 */}
      <section style={sectionStyle}>
        <h2 style={h2Style}>合作房源实拍</h2>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <Image
              key={i}
              src={`/tuoguan/case-${i}.jpg`}
              alt={`观海居托管房源实拍 ${i}`}
              width={450}
              height={300}
              style={{ width: "100%", height: "auto", borderRadius: 10, display: "block" }}
            />
          ))}
        </div>
      </section>

      {/* 7. 留资表单 */}
      <section
        id="lead-form"
        style={{ ...sectionStyle, background: NAVY, maxWidth: "none" }}
      >
        <div
          style={{
            maxWidth: 480,
            margin: "0 auto",
            background: "#fff",
            borderRadius: 16,
            padding: 24,
          }}
        >
          <h2 style={{ ...h2Style, marginBottom: 8 }}>免费评估您的房子</h2>
          <p style={{ fontSize: 14, color: "#666", textAlign: "center", margin: "0 0 20px" }}>
            留下联系方式，托管顾问 24 小时内为您测算收益
          </p>
          <LeadForm />
        </div>
      </section>

      {/* 8. 页脚 — 备案信息（合规公示） */}
      <footer
        style={{
          background: "#16334E",
          padding: "22px 20px 30px",
          textAlign: "center",
        }}
      >
        <p style={{ fontSize: 12, color: "rgba(255,255,255,0.6)", margin: "0 0 6px" }}>
          青岛观海居房屋管理有限公司
        </p>
        <a
          href="https://beian.miit.gov.cn/"
          target="_blank"
          rel="noopener noreferrer"
          style={{ fontSize: 12, color: "rgba(255,255,255,0.6)", textDecoration: "none" }}
        >
          鲁ICP备2026034162号-1
        </a>
      </footer>

      {/* 9. 悬浮底栏 */}
      <div
        style={{
          position: "fixed",
          left: 0,
          right: 0,
          bottom: 0,
          display: "flex",
          background: "#fff",
          boxShadow: "0 -2px 10px rgba(0,0,0,0.1)",
          zIndex: 50,
        }}
      >
        <a
          href={`tel:${TUOGUAN.phone}`}
          style={{
            flex: 1,
            height: 56,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 16,
            fontWeight: 600,
            color: NAVY,
            textDecoration: "none",
          }}
        >
          ☎ 一键拨号
        </a>
        <a
          href="#lead-form"
          style={{
            flex: 1.4,
            height: 56,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 16,
            fontWeight: 700,
            color: "#fff",
            background: ORANGE,
            textDecoration: "none",
          }}
        >
          免费评估我的房子
        </a>
      </div>
    </main>
  );
}
