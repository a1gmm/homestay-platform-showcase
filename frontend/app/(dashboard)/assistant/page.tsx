"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Space, Spin, Typography } from "antd";
import {
  BarChartOutlined,
  CalendarOutlined,
  FileSearchOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
} from "@ant-design/icons";
import { assistantApi, extractErrorMessage, type AssistantAnswer } from "@/lib/api";
import AssistantAnswerView from "@/components/assistant/AssistantAnswerView";

const { Title, Paragraph, Text } = Typography;

const CAPABILITIES = [
  {
    title: "经营数据",
    description: "查订单数、营业额、净收入、房间营收排名",
    icon: <BarChartOutlined />,
    questions: [
      "上个月营业额和净收入分别是多少？",
      "这个月各房间营收排名怎么样？",
    ],
  },
  {
    title: "订单追溯",
    description: "还原一笔订单的后台操作时间、人员和改动",
    icon: <FileSearchOutlined />,
    questions: [
      "7月29日入住的某位客人订单是谁调整的？",
      "某笔订单发生过哪些后台操作？",
    ],
  },
  {
    title: "今日运营",
    description: "看今日入住、退房、在住、待清洁和保洁工作量",
    icon: <CalendarOutlined />,
    questions: [
      "今天有多少入住、退房和待清洁房间？",
      "上周每位保洁分别打扫了多少间？",
    ],
  },
];

interface ConversationTurn {
  question: string;
  answer?: AssistantAnswer;
  error?: string;
}

export default function AssistantPage() {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ConversationTurn[]>([]);

  const mutation = useMutation<AssistantAnswer, unknown, {
    question: string;
    history: Array<{ role: "user" | "assistant"; content: string }>;
  }>({
    mutationFn: async ({ question: q, history }) => (await assistantApi.ask(q, history)).data,
  });

  const submit = (q: string) => {
    const text = q.trim();
    if (!text || mutation.isPending) return;
    const history = turns
      .filter((turn) => turn.answer)
      .flatMap((turn) => [
        { role: "user" as const, content: turn.question },
        { role: "assistant" as const, content: turn.answer!.summary },
      ])
      .slice(-6);
    setQuestion("");
    setTurns((current) => [...current, { question: text }]);
    mutation.mutate(
      { question: text, history },
      {
        onSuccess: (answer) => {
          setTurns((current) => current.map((turn, index) =>
            index === current.length - 1 ? { ...turn, answer } : turn));
        },
        onError: (error) => {
          const message = extractErrorMessage(error) || "请稍后重试";
          setTurns((current) => current.map((turn, index) =>
            index === current.length - 1 ? { ...turn, error: message } : turn));
        },
      },
    );
  };

  const hasConversation = turns.length > 0;
  const composer = (
    <Card
      variant="borderless"
      style={{
        marginTop: hasConversation ? 18 : 0,
        marginBottom: 24,
        border: "1px solid #e8dfd2",
        background: "linear-gradient(135deg, #fffdf8 0%, #f7f1e7 100%)",
        boxShadow: "0 10px 32px rgba(75, 62, 44, 0.07)",
      }}
      styles={{ body: { padding: 20 } }}
    >
      <Text strong style={{ display: "block", marginBottom: 10, fontSize: 15 }}>
        {hasConversation ? "继续追问" : "你现在想了解什么？"}
      </Text>
      <Space.Compact style={{ width: "100%" }}>
        <Input.TextArea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={hasConversation ? "直接回复“对”，或继续补充时间、客人、房号…" : "例如：上个月营业额和净收入分别是多少？"}
          autoSize={{ minRows: hasConversation ? 1 : 2, maxRows: 5 }}
          className="text-base"
          style={{ fontSize: 16, padding: "12px 14px" }}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              submit(question);
            }
          }}
          aria-label="问题输入框"
        />
        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={mutation.isPending}
          onClick={() => submit(question)}
          style={{ height: "auto", minWidth: 104, fontWeight: 600 }}
        >
          {hasConversation ? "发送" : "开始查询"}
        </Button>
      </Space.Compact>
      <div style={{ marginTop: 10, display: "flex", gap: 6, alignItems: "center", color: "#7b746a" }}>
        <SafetyCertificateOutlined />
        <Text type="secondary" style={{ fontSize: 13 }}>
          只读查询，不会自动修改订单、金额或房态
        </Text>
      </div>
    </Card>
  );

  return (
    <div style={{ maxWidth: 1040, margin: "0 auto", padding: "24px 16px 48px" }}>
      <div style={{ textAlign: "center", marginBottom: 22 }}>
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 14,
            margin: "0 auto 12px",
            display: "grid",
            placeItems: "center",
            background: "#2f2b26",
            color: "#fff",
            fontSize: 21,
          }}
        >
          <RobotOutlined />
        </div>
        <Title level={2} aria-label="经营助手" style={{ margin: 0, fontSize: 28 }}>
          经营助手
        </Title>
        <Paragraph type="secondary" style={{ margin: "8px auto 0", maxWidth: 620, fontSize: 15 }}>
          直接问经营数据、订单经过和今日运营情况。数字由系统实时计算，AI 负责理解问题并把结果说明白。
        </Paragraph>
      </div>

      {!hasConversation && composer}

      {!hasConversation && (
        <>
          <Title level={4} style={{ margin: "0 0 12px", fontSize: 17 }}>
            我能帮你做什么
          </Title>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))",
              gap: 14,
              marginBottom: 20,
            }}
          >
            {CAPABILITIES.map((capability) => (
              <Card
                key={capability.title}
                size="small"
                style={{ borderColor: "#e8dfd2", height: "100%" }}
                styles={{ body: { padding: 16 } }}
              >
                <div style={{ display: "flex", gap: 10, alignItems: "flex-start", marginBottom: 12 }}>
                  <span style={{ color: "#8a6742", fontSize: 19, lineHeight: 1.2 }}>{capability.icon}</span>
                  <div>
                    <Text strong style={{ display: "block", fontSize: 15 }}>{capability.title}</Text>
                    <Text type="secondary" style={{ fontSize: 13 }}>{capability.description}</Text>
                  </div>
                </div>
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  {capability.questions.map((example) => (
                    <Button
                      key={example}
                      type="text"
                      block
                      onClick={() => submit(example)}
                      style={{
                        height: "auto",
                        minHeight: 38,
                        padding: "8px 10px",
                        textAlign: "left",
                        whiteSpace: "normal",
                        background: "#faf7f1",
                        color: "#4c463e",
                      }}
                    >
                      {example}
                    </Button>
                  ))}
                </Space>
              </Card>
            ))}
          </div>

          <div style={{ padding: "14px 16px", borderRadius: 10, background: "#f6f3ee", color: "#655e55" }}>
            <Text strong>怎样问得更准确？</Text>
            <Text type="secondary" style={{ marginLeft: 10 }}>
              尽量带上时间范围、客人姓名或房号，例如“上个月”“某位客人 7 月 29 日入住的订单”“302 房”。
            </Text>
          </div>
        </>
      )}

      {hasConversation && (
        <div aria-label="对话记录">
          {turns.map((turn, index) => (
            <div key={`${index}-${turn.question}`} style={{ marginBottom: 18 }}>
              <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
                <div style={{ maxWidth: "78%", padding: "10px 14px", borderRadius: "14px 14px 4px 14px", background: "#2f2b26", color: "white" }}>
                  {turn.question}
                </div>
              </div>
              {turn.answer && <AssistantAnswerView answer={turn.answer} />}
              {turn.error && <Alert type="error" showIcon message="AI 暂时不可用" description={turn.error} />}
              {!turn.answer && !turn.error && index === turns.length - 1 && (
                <div style={{ padding: "8px 2px" }}><Spin size="small" /> <Text type="secondary">正在查询…</Text></div>
              )}
            </div>
          ))}
          {composer}
        </div>
      )}
    </div>
  );
}
