# Prompt: Add Token Intelligence Tab to ORVIX Playground

## Context

The ORVIX frontend is a Next.js 14 app at `/opt/orvix/frontend/` on the `projecteon` server. The playground page (`/playground`) already has 3 tabs: **Chat**, **Image**, **Video**. Each tab is a self-contained component rendered inside a `Playground` component that manages tab switching.

The backend now has these token intelligence API endpoints (all authenticated with JWT or API key, rate-limited in the `intel` bucket):

```
GET /v1/tokens/{ca}                    → token profile (metadata, supply, price, liquidity, risk)
GET /v1/tokens/{ca}/accumulation       → accumulation score 0-100 + metrics
GET /v1/tokens/{ca}/holders            → top holders resolved to wallets
GET /v1/tokens/{ca}/early-buyers       → first-buy evidence for top holders
GET /v1/tokens/{ca}/social             → DexScreener + Twitter social analysis
GET /v1/tokens/{ca}/clusters           → coordinated wallet cluster detection
GET /v1/tokens/{ca}/intelligence       → AI narrative from GPU node
```

Base URL comes from the existing Redux store config (`apiUrl`).

## Task

Add a 4th tab **"Intel"** (icon: `Search` from lucide-react) to the playground. When selected, it renders a `TokenIntel` component that lets users paste a Solana token CA and see intelligence across 5 sub-tabs.

## Existing Patterns to Follow

The codebase uses these patterns (MUST match exactly):

**Tab switching** (from the Playground component):
```tsx
const tabs = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "image", label: "Image", icon: Image },
  { id: "video", label: "Video", icon: Clapperboard },
];
// Tab buttons use: bg-bg-tertiary text-text-primary for active, text-text-secondary hover:text-text-primary for inactive
```

**PageHeader component** (`components/PageHeader`):
```tsx
<PageHeader title="Playground" subtitle="..." actions={...} />
```

**Button component** (`components/Button`):
```tsx
<Button variant="primary" className="w-full" onClick={...} disabled={...}>
  {loading ? <><Loader2 size={14} className="animate-spin" /> Analyzing…</> : <><Search size={14} /> Analyze</>}
</Button>
```

**Badge component** (`components/Badge`):
```tsx
<Badge className="border-border-strong text-text-secondary">label</Badge>
```

**Design tokens** (Tailwind classes):
- Backgrounds: `bg-bg-primary`, `bg-bg-secondary`, `bg-bg-tertiary`
- Text: `text-text-primary`, `text-text-secondary`, `text-text-tertiary`, `text-text-muted`
- Borders: `border-border`, `border-border-strong`, `border-dashed`
- Accent: `text-accent`, `bg-accent`
- Danger: `text-danger`
- Warning: `text-warning`, `border-warning/30`, `bg-warning/5`
- Success: `text-success`

**Input styling**:
```tsx
className="w-full rounded-md border border-border bg-bg-tertiary px-3 py-2 text-sm font-mono text-text-primary focus:border-accent focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
```

**Card pattern**:
```tsx
<div className="rounded-lg border border-border bg-bg-secondary p-4">
  <div className="mb-3 text-[11px] font-medium uppercase tracking-wide text-text-muted">Section Title</div>
  {/* content */}
</div>
```

**API calls**: Use `fetch` directly with the `apiUrl` from config and the auth token from Redux store (same pattern as Chat/Image/Video tabs).

**Error handling pattern**:
```tsx
// Show error state with TriangleAlert icon, headline, detail, retry button
```

**Loading pattern**:
```tsx
// Loader2 spinner with "Analyzing…" text
```

## Implementation

### 1. Modify the Playground component

In the `Playground` component (exported as `ez` in the minified bundle, source is in `app/playground/page.tsx`):

- Add `{ id: "intel", label: "Intel", icon: Search }` to the `tabs` array
- Add `"intel"` to the `allModes` array
- Add conditional render: `{mode === "intel" && <TokenIntel />}`

### 2. Create `TokenIntel` component

File: `app/playground/token-intel.tsx`

**State:**
```tsx
const [ca, setCa] = useState("");
const [loading, setLoading] = useState(false);
const [error, setError] = useState<string | null>(null);
const [activeTab, setActiveTab] = useState("overview");
const [data, setData] = useState<{
  scan: TokenScan | null;
  accumulation: Accumulation | null;
  holders: Holders | null;
  earlyBuyers: EarlyBuyer[] | null;
  social: SocialAnalysis | null;
  clusters: ClusterAnalysis | null;
  intelligence: Intelligence | null;
} | null>(null);
```

**Sub-tabs** (horizontal pill buttons, same style as playground main tabs):
```
Overview | Social | Holders | Early Buyers | AI Intelligence
```

**Input section:**
- Large input: "Paste a token CA (e.g. So11111111111111111111111111111111111111112)"
- "Analyze" button (primary, full width)
- Example CAs as clickable chips below (use 2-3 well-known Solana tokens)
- Recent searches (stored in state, max 5)

**On submit:**
1. Validate the CA is a valid base58 Solana address (32-44 chars)
2. Fire ALL requests in parallel with `Promise.allSettled`:
   ```tsx
   const [scanRes, accRes, holdersRes, buyersRes, socialRes, clustersRes, intelRes] = 
     await Promise.allSettled([
       fetch(`${apiUrl}/v1/tokens/${ca}`, { headers }),
       fetch(`${apiUrl}/v1/tokens/${ca}/accumulation`, { headers }),
       fetch(`${apiUrl}/v1/tokens/${ca}/holders`, { headers }),
       fetch(`${apiUrl}/v1/tokens/${ca}/early-buyers`, { headers }),
       fetch(`${apiUrl}/v1/tokens/${ca}/social`, { headers }),
       fetch(`${apiUrl}/v1/tokens/${ca}/clusters`, { headers }),
       fetch(`${apiUrl}/v1/tokens/${ca}/intelligence`, { headers }),
     ]);
   ```
3. Each result is independent — partial failures show what succeeded, failed ones show a retry hint
4. Intelligence endpoint may take longer (GPU dispatch) — show a separate loading state for that sub-tab

### 3. Sub-tab: Overview

Shows data from `GET /v1/tokens/{ca}` + `GET /v1/tokens/{ca}/accumulation`:

**Layout:** Two-column grid on desktop, stacked on mobile.

**Left column — Token Profile card:**
- Token name + symbol (large text)
- Metadata URI as clickable link
- Price in USDC (formatted, e.g. "$0.0045")
- Supply (formatted with commas)
- Risk warnings as red badges

**Right column — Accumulation card:**
- Score gauge: circular progress (0-100) with color gradient
  - 0-39: red (`bg-danger`)
  - 40-69: yellow (`bg-warning`)
  - 70-100: green (`bg-success`)
- Label badge: distribution/weak/moderate/strong
- Component breakdown as horizontal bars:
  - Inflow score
  - Activity score
  - Distribution score

**Bottom — Liquidity card:**
- Pool count
- Estimated USDC liquidity (formatted)

### 4. Sub-tab: Social

Shows data from `GET /v1/tokens/{ca}/social`:

**Layout:** Single column with cards.

**Social Score card:**
- Large circular gauge (0-100) with the score
- Color: same gradient as accumulation

**Metrics grid (2x2):**
- DexScreener Trending: green check / red X badge
- 24h Volume: formatted USD number
- 24h Price Change: green/red with arrow icon
- Twitter Followers: formatted number or "N/A"

**Sentiment card:**
- Badge: positive (green) / neutral (yellow) / negative (red)
- Brief explanation text

**Social Links card:**
- Clickable icons/links for: Twitter, Website, Telegram, Discord
- Show "Not available" for missing links

### 5. Sub-tab: Holders & Clusters

Shows data from `GET /v1/tokens/{ca}/holders` + `GET /v1/tokens/{ca}/clusters`:

**Holders section:**
- Top-10 share as a donut chart (using Recharts `PieChart`)
  - "Top 10" slice vs "Others" slice
  - Colors: accent for top 10, bg-tertiary for others
- Holders table (responsive — cards on mobile):
  | Rank | Wallet | Balance | % Supply |
  - Wallet: truncated (first 4 + last 4 chars) with copy button
  - Balance: formatted number
  - % Supply: calculated from total

**Clusters section:**
- If no clusters: "No coordinated clusters detected" with a check icon
- For each cluster, a card showing:
  - Cluster ID
  - Wallet addresses (truncated, horizontal list)
  - Signal badges:
    - `shared_funding` — blue badge
    - `coordinated_timing` — yellow badge
    - `overlapping_holdings` — purple badge
  - Confidence bar (0-1) with color:
    - 0-0.33: green (low risk)
    - 0.34-0.67: yellow (medium)
    - 0.68-1.0: red (high — potential coordinated activity)
  - Warning icon if confidence > 0.6

### 6. Sub-tab: Early Buyers

Shows data from `GET /v1/tokens/{ca}/early-buyers`:

**Timeline visualization:**
- Vertical timeline with dots connected by a line
- Each entry shows:
  - Wallet (truncated) with copy button
  - Amount bought
  - Timestamp (relative, e.g. "3 days ago")
  - Transaction signature as a link to Solscan: `https://solscan.io/tx/{signature}`
- Sorted oldest first (top = earliest buyer)

**If empty:** "No early buyer data available for this token"

### 7. Sub-tab: AI Intelligence

Shows data from `GET /v1/tokens/{ca}/intelligence`:

**Loading state** (while GPU node processes):
- Animated brain/GPU icon
- "ORVIX AI is analyzing this token…"
- "Powered by the ORVX GPU Compute Network"
- Subtle pulse animation

**Result card:**
- Narrative: the AI-written market picture (2-4 sentences), styled as a quote block with accent left border
- Risk Flags: red badges in a flex wrap
- Watch Next: highlighted box with the recommendation text
- Footer: model name, latency, node ID, timestamp

**Error states:**
- 503 no_chat_provider: "No GPU nodes available for AI analysis. The network may be processing other requests."
- 503 capacity_exhausted: "All GPU nodes are busy. Retrying may help."
- Other errors: generic retry prompt

**Powered by footer:**
```tsx
<p className="flex items-center gap-2 text-xs text-text-muted">
  <Cpu size={12} /> Powered by ORVX GPU Compute Network
</p>
```

### 8. Shared Components to Create

**`ScoreGauge`** — reusable circular progress for accumulation and social scores:
```tsx
function ScoreGauge({ score, label, size = 120 }: { score: number; label?: string; size?: number }) {
  // SVG circle with stroke-dasharray animation
  // Color gradient based on score value
  // Center text shows the number
  // Optional label below
}
```

**`TruncatedAddress`** — wallet/address display with copy:
```tsx
function TruncatedAddress({ address, chars = 4 }: { address: string; chars?: number }) {
  // Shows first N + "..." + last N chars
  // Click to copy button
  // Tooltip with full address
}
```

**`MetricCard`** — labeled value display:
```tsx
function MetricCard({ label, value, icon, trend }: { label: string; value: string; icon?: ReactNode; trend?: "up" | "down" | "neutral" }) {
  // Label on top, value below
  // Optional icon
  // Trend color (green up, red down)
}
```

### 9. Responsive Behavior

- **Mobile (< 768px):** Sub-tabs scroll horizontally. Tables become card layouts. Two-column grids stack.
- **Tablet (768-1024px):** Two-column grids remain. Tables stay.
- **Desktop (> 1024px):** Full layout with sidebar-style sub-tab navigation.

### 10. URL State

Like the existing tabs, support `?mode=intel` in the URL. Also support `?ca=<address>` to deep-link to a specific token analysis.

## Files to Create/Modify

| Action | File |
|--------|------|
| **Modify** | `app/playground/page.tsx` — add Intel tab |
| **Create** | `app/playground/token-intel.tsx` — main TokenIntel component |
| **Create** | `app/playground/intel-overview.tsx` — Overview sub-tab |
| **Create** | `app/playground/intel-social.tsx` — Social sub-tab |
| **Create** | `app/playground/intel-holders.tsx` — Holders & Clusters sub-tab |
| **Create** | `app/playground/intel-buyers.tsx` — Early Buyers sub-tab |
| **Create** | `app/playground/intel-ai.tsx` — AI Intelligence sub-tab |
| **Create** | `app/playground/intel-shared.tsx` — ScoreGauge, TruncatedAddress, MetricCard |

## API Response Shapes

For reference, these are the exact JSON shapes the backend returns:

```typescript
// GET /v1/tokens/{ca}
interface TokenScan {
  mint: string;
  metadata: { name: string | null; symbol: string | null; uri: string | null; update_authority: string | null } | null;
  supply: { amount: string; decimals: number; ui_amount_string: string | null } | null;
  price_usdc: number | null;
  liquidity: { estimated_usdc: number | null; pool_count: number };
  holders: { total_holders: number | null; top_holders: Array<{wallet: string; balance: number}>; top10_share: number | null; as_of: string } | null;
  risk: { warnings: string[] };
  scanned_at: string;
}

// GET /v1/tokens/{ca}/accumulation
interface Accumulation {
  mint: string;
  score: number; // 0-100
  label: "distribution" | "weak" | "moderate" | "strong";
  metrics: {
    watchlist_wallets: number;
    inflow_7d: number;
    inflow_ratio: number | null;
    buy_tx_7d: number;
    top10_share: number | null;
    distribution_score: number;
    inflow_score: number;
    activity_score: number;
  };
  computed_at: string;
}

// GET /v1/tokens/{ca}/holders
interface Holders {
  total_holders: number;
  top_holders: Array<{ wallet: string; token_account: string; balance: number }>;
  top10_share: number | null;
  as_of: string;
  source: string;
}

// GET /v1/tokens/{ca}/early-buyers
type EarlyBuyer[] = Array<{
  wallet: string;
  amount: number;
  signature: string;
  block_time: number | null;
}>;

// GET /v1/tokens/{ca}/social
interface SocialAnalysis {
  mint: string;
  social_links: { twitter: string | null; website: string | null; telegram: string | null; discord: string | null };
  social_score: number; // 0-100
  metrics: {
    dex_trending: boolean;
    dex_volume_24h: number | null;
    dex_price_change_24h: number | null;
    twitter_followers: number | null;
    twitter_statuses_7d: number | null;
    social_sentiment: "positive" | "neutral" | "negative" | null;
  };
  as_of: string;
}

// GET /v1/tokens/{ca}/clusters
interface ClusterAnalysis {
  mint: string;
  clusters: Array<{
    id: string;
    wallets: string[];
    signals: Array<"shared_funding" | "coordinated_timing" | "overlapping_holdings">;
    confidence: number; // 0-1
  }>;
  total_wallets_analyzed: number;
  as_of: string;
}

// GET /v1/tokens/{ca}/intelligence
interface Intelligence {
  mint: string;
  model: string;
  analysis: {
    narrative: string;
    risk_flags: string[];
    watch_next: string;
  };
  generated_at: string;
  latency_ms: number;
  node_id: string;
}
```
