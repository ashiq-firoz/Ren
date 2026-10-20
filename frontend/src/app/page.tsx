"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { FullScreenLoader } from "@/components/ui/feedback";
import { useAuth } from "@/providers/auth";

export default function Home() {
  const { status } = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (status === "authenticated" || status === "offline") router.replace("/chat");
    else if (status === "unauthenticated") router.replace("/login");
  }, [status, router]);
  return <FullScreenLoader />;
}
