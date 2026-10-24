// Applied after the user's Global Extend Script main(config, profileName).
// No network or file access: each subscription resolves its own proxy policy.
function __claudeSwitcherApply(config, preferredGroup) {
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error("Claude Switcher: Global Extend Script must return a config object");
  }
  const builtins = ["DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"];
  const validName = name => typeof name === "string" && name.length > 0 &&
    !/[,\r\n]/.test(name) && !builtins.includes(name.toUpperCase());
  const allGroups = (config["proxy-groups"] || []).filter(group =>
    group && validName(group.name) &&
    ["select", "url-test", "fallback", "load-balance", "relay"].includes(group.type));
  const nodes = (config.proxies || []).filter(node =>
    node && validName(node.name) && typeof node.type === "string" &&
    !["direct", "reject", "dns", "compatible"].includes(String(node.type).toLowerCase()));
  const hasProxy = (name, seen) => {
    if (nodes.some(node => node.name === name)) return true;
    if (seen.includes(name)) return false;
    const group = allGroups.find(item => item.name === name);
    if (!group || group["disable-udp"] === true) return false;
    if (Array.isArray(group.use) && group.use.length > 0) return true;
    if (group["include-all"] && (nodes.length || Object.keys(config["proxy-providers"] || {}).length)) return true;
    return (group.proxies || []).some(child => hasProxy(child, seen.concat(name)));
  };
  const groups = allGroups.filter(group => hasProxy(group.name, []));
  const preferred = groups.concat(nodes).find(item => item.name === preferredGroup);
  const fallback = groups.find(group => !/direct|直连|reject|拒绝/i.test(group.name)) || nodes[0];
  // A subscription without a usable policy must block protected traffic.
  const policy = (preferred || fallback || {}).name || "REJECT";
  const domains = [
    "claude.ai", "claude.com", "anthropic.com", "claudeusercontent.com",
    "challenges.cloudflare.com", "datadoghq.com", "segment.io", "stripe.com",
    "stripe.network", "stripecdn.com", "intercom.io", "intercomcdn.com",
    "intercomassets.com", "www.cloudflare.com"
  ];
  const pinned = domains.map(domain => "DOMAIN-SUFFIX," + domain + "," + policy);
  // Literal destinations keep the reference requests IPv4/IPv6 respectively,
  // even when proxy-side DNS would otherwise choose a different address family.
  pinned.push("IP-CIDR,1.1.1.1/32," + policy + ",no-resolve");
  pinned.push("IP-CIDR6,2606:4700:4700::1111/128," + policy + ",no-resolve");
  [3478, 5349, 19302].forEach(port => pinned.push("DST-PORT," + port + "," + policy));
  // Mihomo may skip a proxy rule when its selected node has no UDP support.
  [3478, 5349, 19302].forEach(port => pinned.push("DST-PORT," + port + ",REJECT"));
  const prefix = pinned.filter((rule, index) => pinned.indexOf(rule) === index);
  if (config.rules !== undefined && !Array.isArray(config.rules)) {
    throw new Error("Claude Switcher: config.rules must be an array");
  }
  const previous = config.rules || [];
  config.rules = prefix.concat(previous.filter(rule => !prefix.includes(rule)));
  return config;
}
