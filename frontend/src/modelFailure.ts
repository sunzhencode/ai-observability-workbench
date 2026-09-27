/**
 * Wording for a failed model call, in one testable place.
 *
 * This module exists because the same defect happened twice. Stage 1 shipped a
 * whole taxonomy of evidence failures and then did not render the one field
 * that told them apart, so every problem read as "读取失败" (fixed in
 * `bd21c07`). The model channel then did it again: a real 404 from a mistyped
 * model name surfaced as a bare `SERVICE_ERROR`, which sends the reader to
 * check their network when the fix is a dropdown two fields up.
 *
 * The rule the taxonomy is only worth having for: **each entry must name a
 * different next action.** If two codes would produce the same sentence, they
 * did not need to be two codes.
 */

const KIND_TEXT: Record<string, string> = {
  EGRESS_REJECTED: "地址被出站护栏拒绝：只允许公网 HTTPS，不跟随跳转",
  NOT_CONFIGURED: "配置不完整，检查 Base URL 与 API key",
  UNREACHABLE: "连不上这个地址，检查 Base URL 和网络",
  TIMEOUT: "服务超时",
  AUTH_FAILED: "服务拒绝了这个 API key",
  RATE_LIMITED: "服务限流，稍后再试",
  SERVICE_ERROR: "服务返回了错误",
  RESPONSE_TOO_LARGE: "返回内容超出上限",
  MALFORMED_RESPONSE: "返回的内容不是预期结构",
  CONTRACT_INVALID: "模型没能产出要求的结构化结论——换一个能力更强的模型试试",
};

/**
 * What an HTTP status means *here*, which is not what it means in general.
 *
 * 404 on a chat-completions call is almost never "the service is down"; it is
 * "that model name does not exist on this service". Saying so is the whole
 * difference between a dead end and a two-second fix.
 */
const STATUS_TEXT: Record<string, string> = {
  HTTP_400: "请求被拒绝（400）——模型名不被接受，或这个模型不收我们发的参数。先点「拉取可用模型」确认名字",
  HTTP_401: "API key 无效或已过期（401）",
  HTTP_403: "这个 key 没有访问权限（403）",
  HTTP_404: "找不到这个模型（404）——点「拉取可用模型」从下拉里选",
  HTTP_429: "超出配额或频率限制（429）",
  HTTP_500: "服务内部错误（500）",
  HTTP_502: "网关错误（502）",
  HTTP_503: "服务暂时不可用（503）",
};

const EGRESS_TEXT: Record<string, string> = {
  EGRESS_SCHEME: "地址必须是 https",
  EGRESS_PORT: "只允许 443 端口",
  EGRESS_USERINFO: "地址里不能带用户名密码",
  EGRESS_NO_HOST: "地址里没有主机名",
  EGRESS_DNS: "这个主机名解析不出地址",
  EGRESS_PRIVATE_ADDRESS: "这个主机名解析到了内网地址，已拒绝",
  EGRESS_KIND: "这个通道类型没有对应的出站策略",
};

/**
 * One sentence a reader can act on.
 *
 * The sub-code wins when it says something more specific than the kind — a
 * `SERVICE_ERROR` carrying `HTTP_404` is a mistyped model, and reporting the
 * kind alone throws that away. The raw codes stay appended so a bug report can
 * still quote something exact.
 */
export function modelFailureText(kind: string, detail = ""): string {
  const specific = STATUS_TEXT[detail] ?? EGRESS_TEXT[detail] ?? "";
  const general = KIND_TEXT[kind] ?? kind;
  if (specific) return `${specific}（${kind}${detail ? ` / ${detail}` : ""}）`;
  return detail ? `${general}（${detail}）` : general;
}
