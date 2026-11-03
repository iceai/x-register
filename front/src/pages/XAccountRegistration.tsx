import { RegisterPage } from "@/pages/Register";

/** X.com（原 Twitter）账号注册的独立入口。 */
export function XAccountRegistrationPage({ view }: { view: "new" | "runtime" }) {
  return <RegisterPage view={view} platform="x" />;
}
