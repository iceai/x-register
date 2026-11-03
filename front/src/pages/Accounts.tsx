import { useEffect, useMemo, useState } from "react";
import { Copy, Eye, RefreshCw, Search, Trash2, X } from "lucide-react";
import { api, type AccountRecord } from "@/lib/api";
import {
  Badge,
  Button,
  Card,
  Input,
  PageHeader,
  PaginationBar,
  Select,
  Toast,
} from "@/components/ui";

function statusVariant(status: string) {
  if (status === "success") return "success" as const;
  if (status === "failure") return "destructive" as const;
  if (status === "cancelled") return "warning" as const;
  return "secondary" as const;
}

function statusLabel(status: string) {
  return {
    success: "成功",
    failure: "失败",
    cancelled: "已取消",
    skipped: "已跳过",
  }[status] || status || "未知";
}

function platformLabel(platform: string) {
  return String(platform || "xai").toLowerCase() === "x" ? "X.com" : "xAI / Grok";
}

function DetailDialog({ item, onClose }: { item: AccountRecord; onClose: () => void }) {
  const rows: Array<[string, string | number]> = [
    ["账号平台", platformLabel(item.platform)],
    ["邮箱", item.email || "—"],
    ["密码", item.password || "—"],
    ["状态", statusLabel(item.status)],
    ["邮箱服务", item.provider || "—"],
    ["开始时间", item.started_at || "—"],
    ["完成时间", item.finished_at || "—"],
    ["耗时", item.duration_seconds ? `${item.duration_seconds.toFixed(1)} 秒` : "—"],
    ["批次", item.batch_id || "—"],
    ["失败类型", item.failure_type || item.exception_type || "—"],
    ["失败原因", item.failure_reason || "—"],
    ["出口 IP", item.exit_ip || "—"],
    ["账号文件", item.account_file || "—"],
  ];

  return (
    <div className="fixed inset-0 z-[80] flex items-end justify-center bg-slate-950/45 p-0 backdrop-blur-sm sm:items-center sm:p-5" role="dialog" aria-modal="true" aria-label="账号详情">
      <div className="max-h-[92dvh] w-full max-w-3xl overflow-y-auto rounded-t-2xl bg-white shadow-2xl sm:rounded-2xl">
        <div className="sticky top-0 flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 sm:px-5">
          <div>
            <h2 className="font-semibold text-slate-950">账号详情</h2>
            <p className="mt-0.5 text-xs text-slate-500">{item.email || `记录 #${item.id}`}</p>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭详情">
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="grid gap-x-6 gap-y-0 p-4 sm:grid-cols-2 sm:p-5">
          {rows.map(([label, value]) => (
            <div key={label} className="min-w-0 border-b border-slate-100 py-3">
              <div className="text-xs text-slate-400">{label}</div>
              <div className="mt-1 break-all text-sm text-slate-800">{value}</div>
            </div>
          ))}
        </div>
        {item.exception_traceback ? (
          <div className="mx-4 mb-4 sm:mx-5 sm:mb-5">
            <div className="mb-2 text-xs font-medium text-slate-500">异常堆栈</div>
            <pre className="max-h-64 overflow-auto rounded-xl bg-slate-950 p-4 text-xs leading-5 text-slate-100">{item.exception_traceback}</pre>
          </div>
        ) : null}
        {item.screenshot_url ? (
          <div className="mx-4 mb-4 sm:mx-5 sm:mb-5">
            <div className="mb-2 text-xs font-medium text-slate-500">失败截图</div>
            <img src={item.screenshot_url} alt="注册失败现场" className="max-h-[55vh] w-full rounded-xl border border-slate-200 object-contain" />
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function AccountsPage() {
  const [items, setItems] = useState<AccountRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("");
  const [platform, setPlatform] = useState("");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<number[]>([]);
  const [detail, setDetail] = useState<AccountRecord | null>(null);
  const [toast, setToast] = useState<{ message: string; tone: "success" | "error" }>({ message: "", tone: "success" });

  const showToast = (message: string, tone: "success" | "error" = "success") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast((value) => ({ ...value, message: "" })), 2600);
  };

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.accounts({
        status,
        platform,
        q: appliedQuery,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setItems(data.items || []);
      setTotal(data.total || 0);
      setSelected([]);
    } catch (error: any) {
      showToast(error.message || "账号列表加载失败", "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [status, platform, appliedQuery, page, pageSize]);

  const pageIds = useMemo(() => items.map((item) => item.id), [items]);
  const allSelected = !!pageIds.length && pageIds.every((id) => selected.includes(id));

  const togglePage = () => {
    setSelected(allSelected ? selected.filter((id) => !pageIds.includes(id)) : Array.from(new Set([...selected, ...pageIds])));
  };

  const deleteRows = async (ids: number[]) => {
    if (!ids.length || !window.confirm(`删除选中的 ${ids.length} 条账号记录及其账号文件？`)) return;
    try {
      const result = await api.deleteAccounts(ids, true);
      showToast(`已删除 ${result.deleted} 条记录`);
      if (detail && ids.includes(detail.id)) setDetail(null);
      await load();
    } catch (error: any) {
      showToast(error.message || "删除失败", "error");
    }
  };

  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
      showToast(`${label}已复制`);
    } catch {
      showToast("复制失败，请手动复制", "error");
    }
  };

  return (
    <div className="space-y-5 sm:space-y-6">
      <Toast message={toast.message} tone={toast.tone} />
      <PageHeader
        title="账号管理"
        description="统一管理 xAI/Grok 与 X.com 注册产生的账号、失败记录和运行现场。"
        actions={
          <>
            {selected.length ? (
              <Button variant="destructive" onClick={() => void deleteRows(selected)}>
                <Trash2 className="h-4 w-4" />
                删除所选（{selected.length}）
              </Button>
            ) : null}
            <Button variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
              刷新
            </Button>
          </>
        }
      />

      <Card className="overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-slate-200 p-4 sm:flex-row sm:items-center">
          <form
            className="flex min-w-0 flex-1 gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              setPage(1);
              setAppliedQuery(query.trim());
            }}
          >
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
              <Input value={query} onChange={(event) => setQuery(event.target.value)} className="pl-9" placeholder="搜索邮箱、失败原因或服务商" />
            </div>
            <Button type="submit" variant="secondary">搜索</Button>
          </form>
          <Select
            className="w-full sm:w-36"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setPage(1);
            }}
          >
            <option value="">全部状态</option>
            <option value="success">成功</option>
            <option value="failure">失败</option>
            <option value="cancelled">已取消</option>
          </Select>
          <Select
            className="w-full sm:w-40"
            value={platform}
            onChange={(event) => {
              setPlatform(event.target.value);
              setPage(1);
            }}
          >
            <option value="">全部平台</option>
            <option value="xai">xAI / Grok</option>
            <option value="x">X.com</option>
          </Select>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[1000px] text-left text-sm">
            <thead className="bg-slate-50 text-xs font-medium text-slate-500">
              <tr>
                <th className="w-12 px-4 py-3"><input type="checkbox" checked={allSelected} onChange={togglePage} aria-label="选择当前页" /></th>
                <th className="px-3 py-3">邮箱</th>
                <th className="px-3 py-3">平台</th>
                <th className="px-3 py-3">密码</th>
                <th className="px-3 py-3">状态</th>
                <th className="px-3 py-3">邮箱服务</th>
                <th className="px-3 py-3">完成时间</th>
                <th className="px-3 py-3">结果</th>
                <th className="w-32 px-4 py-3 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((item) => (
                <tr key={item.id} className="hover:bg-slate-50/70">
                  <td className="px-4 py-3"><input type="checkbox" checked={selected.includes(item.id)} onChange={() => setSelected((ids) => ids.includes(item.id) ? ids.filter((id) => id !== item.id) : [...ids, item.id])} aria-label={`选择 ${item.email || item.id}`} /></td>
                  <td className="max-w-[250px] px-3 py-3">
                    <div className="truncate font-medium text-slate-900">{item.email || "—"}</div>
                    <div className="mt-0.5 text-xs text-slate-400">#{item.id}</div>
                  </td>
                  <td className="px-3 py-3"><Badge variant="secondary">{platformLabel(item.platform)}</Badge></td>
                  <td className="px-3 py-3">
                    {item.password ? (
                      <Button variant="ghost" size="sm" className="font-mono" onClick={() => void copy(item.password, "密码")}>
                        ••••••••
                        <Copy className="h-3.5 w-3.5" />
                      </Button>
                    ) : "—"}
                  </td>
                  <td className="px-3 py-3"><Badge variant={statusVariant(item.status)}>{statusLabel(item.status)}</Badge></td>
                  <td className="px-3 py-3 text-slate-600">{item.provider || "—"}</td>
                  <td className="whitespace-nowrap px-3 py-3 text-xs text-slate-500">{item.finished_at || "—"}</td>
                  <td className="max-w-[260px] px-3 py-3">
                    <div className="truncate text-slate-600">{item.failure_reason || (item.success ? "注册完成" : "—")}</div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setDetail(item)}><Eye className="h-4 w-4" />详情</Button>
                      <Button variant="ghost" size="sm" className="text-red-600" onClick={() => void deleteRows([item.id])}><Trash2 className="h-4 w-4" /></Button>
                    </div>
                  </td>
                </tr>
              ))}
              {!loading && !items.length ? (
                <tr><td colSpan={9} className="px-4 py-16 text-center text-slate-500">暂无账号记录</td></tr>
              ) : null}
              {loading ? (
                <tr><td colSpan={9} className="px-4 py-16 text-center text-slate-500">正在加载…</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <PaginationBar
          page={page}
          pageSize={pageSize}
          total={total}
          loading={loading}
          onPageChange={setPage}
          onPageSizeChange={(value) => {
            setPageSize(value);
            setPage(1);
          }}
        />
      </Card>

      {detail ? <DetailDialog item={detail} onClose={() => setDetail(null)} /> : null}
    </div>
  );
}
