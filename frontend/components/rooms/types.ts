import type { RoomOut } from "@/lib/types";

export interface PricingDay {
  date: string;
  price: number | null;
  source: string;
  source_url: string | null;
}

export interface PricingDetail {
  date: string;
  recommended_price: number;
  base_price: number | null;
  competitor_avg_price: number | null;
  factors: Record<string, unknown>;
  competitors: { name?: string | null; price: number }[];
  source_url?: string | null;
}

export type RoomItem = RoomOut;
