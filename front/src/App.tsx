import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { DashboardPage } from "@/pages/Dashboard";
import { AccountsPage } from "@/pages/Accounts";
import { XRegistrationPage } from "@/pages/XRegistration";
import { XAccountRegistrationPage } from "@/pages/XAccountRegistration";
import { SettingsPage } from "@/pages/Settings";
import { api } from "@/lib/api";
import { LoginPage } from "@/pages/Login";
import { ConfigFilePage } from "@/pages/ConfigFile";
import { FlaggedExitIpsPage } from "@/pages/FlaggedExitIps";

export default function App() {
  const [jobRunning, setJobRunning] = useState(false);
  const [jobPolling, setJobPolling] = useState(true);
  const [authLoading, setAuthLoading] = useState(true);
  const [auth, setAuth] = useState({ enabled: false, setup_required: true, authenticated: false });

  useEffect(() => {
    const onAuthRequired = (event: Event) => {
      const setupRequired = !!(event as CustomEvent<{ setupRequired?: boolean }>).detail?.setupRequired;
      setAuth({ enabled: !setupRequired, setup_required: setupRequired, authenticated: false });
    };
    const onJobState = (event: Event) => {
      const running = !!(event as CustomEvent<{ running?: boolean }>).detail?.running;
      setJobRunning(running);
      setJobPolling(running);
    };
    window.addEventListener("x-register-auth-required", onAuthRequired);
    window.addEventListener("x-register-job-state", onJobState);
    api.authMe()
      .then((data) => setAuth({
        enabled: !!data.enabled,
        setup_required: !!data.setup_required,
        authenticated: !!data.authenticated,
      }))
      .catch(() => setAuth({ enabled: true, setup_required: false, authenticated: false }))
      .finally(() => setAuthLoading(false));
    return () => {
      window.removeEventListener("x-register-auth-required", onAuthRequired);
      window.removeEventListener("x-register-job-state", onJobState);
    };
  }, []);

  useEffect(() => {
    if (authLoading || (auth.enabled && !auth.authenticated) || !jobPolling) return;
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const data = await api.jobs();
        if (!alive) return;
        const running = !!data.running;
        setJobRunning(running);
        if (running) timer = window.setTimeout(tick, 3000);
        else setJobPolling(false);
      } catch {
        if (alive) timer = window.setTimeout(tick, 5000);
      }
    };
    void tick();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [authLoading, auth.enabled, auth.authenticated, jobPolling]);

  if (authLoading) {
    return <div className="flex min-h-[100dvh] items-center justify-center text-muted-foreground">加载中…</div>;
  }
  if (auth.setup_required || (auth.enabled && !auth.authenticated)) {
    return <LoginPage setupRequired={!!auth.setup_required} onLoggedIn={() => setAuth({ enabled: true, setup_required: false, authenticated: true })} />;
  }

  const logout = async () => {
    try {
      await api.logout();
    } finally {
      setAuth({ enabled: true, setup_required: false, authenticated: false });
      setJobRunning(false);
      setJobPolling(false);
    }
  };

  return (
    <Routes>
      <Route element={<Layout jobRunning={jobRunning} onLogout={auth.enabled ? logout : undefined} />}>
        <Route index element={<Navigate to="/overview" replace />} />
        <Route path="overview" element={<DashboardPage />} />
        <Route path="accounts" element={<AccountsPage />} />
        <Route path="accounts/exit-ips" element={<FlaggedExitIpsPage />} />
        <Route path="x-registration/new" element={<XRegistrationPage view="new" />} />
        <Route path="x-registration/runtime" element={<XRegistrationPage view="runtime" />} />
        <Route path="x-account-registration/new" element={<XAccountRegistrationPage view="new" />} />
        <Route path="x-account-registration/runtime" element={<XAccountRegistrationPage view="runtime" />} />
        <Route path="registration/new" element={<Navigate to="/x-registration/new" replace />} />
        <Route path="registration/runtime" element={<Navigate to="/x-registration/runtime" replace />} />
        <Route path="register" element={<Navigate to="/x-registration/new" replace />} />
        <Route path="settings/registration" element={<SettingsPage section="registration" />} />
        <Route path="settings/mail" element={<SettingsPage section="mail" />} />
        <Route path="settings/outlook" element={<SettingsPage section="outlook" />} />
        <Route path="settings/config" element={<ConfigFilePage />} />
        <Route path="settings" element={<Navigate to="/settings/registration" replace />} />
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Route>
    </Routes>
  );
}
