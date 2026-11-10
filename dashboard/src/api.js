export async function loadDashboard(signal) {
  const response = await fetch("/api/dashboard", {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error("Dashboard state is temporarily unavailable");
  }
  return response.json();
}
