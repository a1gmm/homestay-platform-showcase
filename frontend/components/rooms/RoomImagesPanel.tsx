"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { extractErrorMessage } from "@/lib/api-errors";
import { Upload, message, Button, Tag, Popconfirm, Empty } from "antd";
import { DeleteOutlined, StarFilled, StarOutlined, PlusOutlined } from "@ant-design/icons";
import type { UploadProps } from "antd/es/upload";
import type { RcFile } from "antd/es/upload/interface";
import { tokens } from "@/lib/design-tokens";

export interface RoomImage {
  image_id: string;
  room_id: string;
  url: string;
  sort_order: number;
  is_cover: boolean;
  content_type: string | null;
  created_at: string;
}

export interface RoomImagesApi {
  list: () => Promise<{ data: RoomImage[] }>;
  upload: (file: File) => Promise<{ data: RoomImage }>;
  patch: (imageId: string, body: { is_cover?: boolean }) => Promise<unknown>;
  delete: (imageId: string) => Promise<unknown>;
}

const ALLOWED_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const MAX_BYTES = 5 * 1024 * 1024;
const MAX_IMAGES = 10;

export function RoomImagesPanel({
  roomId,
  api,
  queryKey,
}: {
  roomId: string;
  api: RoomImagesApi;
  /** TanStack Query cache key; lets parent invalidate from outside if needed. */
  queryKey: readonly unknown[];
}) {
  const qc = useQueryClient();

  const { data: images = [], isLoading } = useQuery({
    queryKey,
    queryFn: async () => (await api.list()).data,
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.upload(file),
    onSuccess: () => {
      message.success("已上传");
      qc.invalidateQueries({ queryKey });
    },
    onError: (err: any) => {
      message.error(extractErrorMessage(err, "上传失败"));
    },
  });

  const patchMutation = useMutation({
    mutationFn: ({ imageId, body }: { imageId: string; body: { is_cover?: boolean } }) =>
      api.patch(imageId, body),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
    onError: (err: any) => {
      message.error(extractErrorMessage(err, "操作失败"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (imageId: string) => api.delete(imageId),
    onSuccess: () => {
      message.success("已删除");
      qc.invalidateQueries({ queryKey });
    },
    onError: (err: any) => {
      message.error(extractErrorMessage(err, "删除失败"));
    },
  });

  const beforeUpload: UploadProps["beforeUpload"] = (file: RcFile) => {
    if (!ALLOWED_TYPES.has(file.type)) {
      message.error("仅支持 JPG / PNG / WEBP 格式");
      return Upload.LIST_IGNORE;
    }
    if (file.size > MAX_BYTES) {
      message.error("单张图片不能超过 5 MB");
      return Upload.LIST_IGNORE;
    }
    if (images.length >= MAX_IMAGES) {
      message.error(`每间房最多 ${MAX_IMAGES} 张图片`);
      return Upload.LIST_IGNORE;
    }
    return true;
  };

  const customRequest: UploadProps["customRequest"] = async (opts) => {
    try {
      await uploadMutation.mutateAsync(opts.file as File);
      opts.onSuccess?.({}, undefined as any);
    } catch (e) {
      opts.onError?.(e as Error);
    }
  };

  return (
    <div>
      <div
        style={{
          fontSize: 12,
          color: tokens.color.text.tertiary,
          marginBottom: 12,
          lineHeight: 1.6,
        }}
      >
        每间房最多 {MAX_IMAGES} 张；单张不超过 5 MB；格式支持 JPG / PNG / WEBP。
        第一张会自动设为封面，点 ★ 可切换。当前 {images.length}/{MAX_IMAGES}。
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))",
          gap: 10,
          marginBottom: 12,
        }}
      >
        {images.map((img) => (
          <ImageCard
            key={img.image_id}
            image={img}
            onSetCover={() =>
              patchMutation.mutate({ imageId: img.image_id, body: { is_cover: true } })
            }
            onDelete={() => deleteMutation.mutate(img.image_id)}
            busy={patchMutation.isPending || deleteMutation.isPending}
          />
        ))}

        {images.length < MAX_IMAGES && (
          <Upload
            listType="picture-card"
            showUploadList={false}
            beforeUpload={beforeUpload}
            customRequest={customRequest}
            accept="image/jpeg,image/png,image/webp"
            multiple
          >
            <div>
              <PlusOutlined />
              <div style={{ marginTop: 8, fontSize: 12 }}>
                {uploadMutation.isPending ? "上传中..." : "上传"}
              </div>
            </div>
          </Upload>
        )}
      </div>

      {!isLoading && images.length === 0 && (
        <Empty description="还没有图片,上传第一张" style={{ margin: "8px 0" }} />
      )}
    </div>
  );
}

function ImageCard({
  image,
  onSetCover,
  onDelete,
  busy,
}: {
  image: RoomImage;
  onSetCover: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  return (
    <div
      style={{
        position: "relative",
        aspectRatio: "1 / 1",
        borderRadius: tokens.radius.md,
        overflow: "hidden",
        border: `0.5px solid #E5DDCB`,
        background: `url("${image.url}") center/cover`,
      }}
    >
      {image.is_cover && (
        <Tag
          color="#8A6E5A"
          style={{
            position: "absolute",
            top: 6,
            left: 6,
            margin: 0,
            fontSize: 11,
          }}
        >
          封面
        </Tag>
      )}
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          right: 0,
          padding: 6,
          background: "linear-gradient(to top, rgba(0,0,0,0.55), transparent)",
          display: "flex",
          justifyContent: "space-between",
          gap: 4,
        }}
      >
        <Button
          size="small"
          type="text"
          disabled={busy || image.is_cover}
          onClick={onSetCover}
          style={{ color: "#fff", padding: "0 4px" }}
          icon={image.is_cover ? <StarFilled /> : <StarOutlined />}
        >
          {image.is_cover ? "封面" : "设为封面"}
        </Button>
        <Popconfirm
          title="确认删除这张图？"
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={onDelete}
        >
          <Button
            size="small"
            type="text"
            disabled={busy}
            style={{ color: "#fff", padding: "0 4px" }}
            icon={<DeleteOutlined />}
          />
        </Popconfirm>
      </div>
    </div>
  );
}
