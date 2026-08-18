"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useStaffStore } from "@/lib/staff-store";

export default function StaffEntry() {
  const router = useRouter();
  const user = useStaffStore((s) => s.user);
  const isLoggedIn = useStaffStore((s) => s.isLoggedIn());

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace("/staff/login");
      return;
    }
    if (user?.role === "cleaner") {
      router.replace("/staff/cleaner");
    } else {
      router.replace("/staff/keeper");
    }
  }, [isLoggedIn, user, router]);

  return null;
}
