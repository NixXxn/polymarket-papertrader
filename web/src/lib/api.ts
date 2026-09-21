import type { DashMode, DashboardPayload } from "./types";

async function readJsonOrThrow<T>(response: Response): Promise<T> {
  const text = await response.text();
  let data: (T & { ok?: boolean; error?: string }) | null = null;
  try {
    data = JSON.parse(text) as T & { ok?: boolean; error?: string };
  } catch {
    const hint = text.trim().startsWith("<")
      ? "Dashboard API returned HTML. Is Flask running on :8787?"
      : `Unexpected API response: ${text.slice(0, 160)}`;
    throw new Error(hint);
  }
  if (!response.ok || (data && data.ok === false)) {
    throw new Error((data && data.error) || `HTTP ${response.status}`);
  }
  return data as T;
}

export async function fetchDashboard(mode: DashMode): Promise<DashboardPayload> {
  const r = await fetch(`/api/dashboard?mode=${encodeURIComponent(mode)}`, {
    cache: "no-store",
  });
  return readJsonOrThrow<DashboardPayload>(r);
}

export async function resetBalances(balance: number, mode: DashMode) {
  const r = await fetch(`/api/reset-balances?mode=${encodeURIComponent(mode)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ balance, mode }),
  });
  return readJsonOrThrow(r);
}

export async function resetStatistics(mode: DashMode) {
  const r = await fetch(
    `/api/reset-statistics?mode=${encodeURIComponent(mode)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    },
  );
  return readJsonOrThrow(r);
}

export async function setStrategyBudget(
  strategy: string,
  balance: number,
  mode: DashMode,
) {
  const r = await fetch(
    `/api/set-strategy-budget?mode=${encodeURIComponent(mode)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy, balance, mode }),
    },
  );
  return readJsonOrThrow(r);
}

export async function addCopyWallet(
  address: string,
  label: string,
  mode: DashMode,
) {
  const r = await fetch(`/api/copy/wallets?mode=${encodeURIComponent(mode)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address, label, mode }),
  });
  return readJsonOrThrow(r);
}

export async function removeCopyWallet(address: string, mode: DashMode) {
  const r = await fetch(`/api/copy/wallets?mode=${encodeURIComponent(mode)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address, mode }),
  });
  return readJsonOrThrow(r);
}
