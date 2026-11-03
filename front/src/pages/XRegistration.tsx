import { RegisterPage } from "@/pages/Register";

/** XRegistration 独立入口；底层复用成熟的注册任务与监控组件。 */
export function XRegistrationPage({ view }: { view: "new" | "runtime" }) {
  return <RegisterPage view={view} platform="xai" />;
}
