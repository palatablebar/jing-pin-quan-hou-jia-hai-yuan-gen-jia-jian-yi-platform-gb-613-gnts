import { api } from '../api.js';
import { can } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, filterBar, kv, table, toast } from '../ui.js';
import { money } from '../components/price.js';

const state = { competitorId: '', reconcileState: '', q: '' };

const STATE_TONE = { passed: 'ok', failed: 'danger' };
const ORDER_STATUS_TONE = { paid: 'info', partially_refunded: 'warn', refunded: 'ok' };
const REFUND_STATE_TONE = { applied: 'ok', invalid: 'danger' };

function checksBlock(checks) {
  if (!checks || !checks.length) return '<p class="muted">没有对账项。</p>';
  return `<div class="table-wrap"><table class="data compact" style="min-width:0">
    <thead><tr><th>对账项</th><th>应为</th><th>实际</th><th>差额</th><th>结论</th></tr></thead>
    <tbody>${checks
      .map(
        (check) => `<tr>
          <td>${esc(check.label)}${check.hint ? `<div class="muted tiny">${esc(check.hint)}</div>` : ''}</td>
          <td class="num money">${money(check.expected)}</td>
          <td class="num money">${money(check.actual)}</td>
          <td class="num ${Number(check.delta) === 0 ? '' : 'deviation'}">${money(check.delta, { sign: true })}</td>
          <td><span class="badge ${check.passed ? 'ok' : 'danger'}">${check.passed ? '通过' : '不通过'}</span></td>
        </tr>`,
      )
      .join('')}</tbody></table></div>`;
}

function itemsBlock(items) {
  if (!items || !items.length) return '<p class="muted">没有货品。</p>';
  return `<div class="table-wrap"><table class="data compact">
    <thead><tr>
      <th>货号</th><th>件数</th><th>成交单价</th><th>成交金额</th><th>整单优惠分摊</th>
      <th>券后净额</th><th>券后单价</th><th>已回冲</th><th>剩余件数</th><th>剩余单价</th>
      <th>旧口径单价</th>
    </tr></thead>
    <tbody>${items
      .map(
        (item) => `<tr>
          <td><b>${esc(item.sku_code)}</b><div class="muted tiny">L${esc(item.line_no)}${item.name ? ` · ${esc(item.name)}` : ''}</div></td>
          <td class="num">${item.qty}</td>
          <td class="num money">${money(item.unit_price)}</td>
          <td class="num money">${money(item.gross_amount)}</td>
          <td class="num money">−${money(item.discount_allocated).replace('¥', '')}</td>
          <td class="num money">${money(item.net_amount)}</td>
          <td class="num money"><b>${money(item.effective_unit_price)}</b></td>
          <td class="num money">${money(item.refunded_amount)}</td>
          <td class="num">${item.remaining_qty}</td>
          <td class="num money">${money(item.remaining_unit_price)}</td>
          <td class="num money muted">${money(item.legacy_unit_price)}</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>
    <p class="muted tiny">「旧口径单价」= 同一张整单券被每个货号全额抵扣后的单价；整单口径下单，这张券只减一次，按成交金额分摊。</p>`;
}

function discountsBlock(discounts) {
  if (!discounts || !discounts.length) return '<p class="muted">这张订单没有整单优惠。</p>';
  return discounts
    .map(
      (discount) => `<div class="alloc-card">
        <div class="ac2-head">
          <b>${esc(discount.seq)}. ${esc(discount.kind_label || discount.kind)}</b>
          <span class="mono muted">${esc(discount.label || '')}</span>
          <span class="money">${money(discount.amount)}</span>
        </div>
        <table class="data compact" style="min-width:0">
          <thead><tr><th>货号行</th><th>分摊权重</th><th>分摊到本行</th><th>已回冲</th><th>剩余分摊</th></tr></thead>
          <tbody>${(discount.allocations || [])
            .map(
              (alloc) => `<tr>
                <td class="num">L${esc(alloc.line_no)}</td>
                <td class="num money">${money(alloc.weight)}</td>
                <td class="num money">${money(alloc.amount)}</td>
                <td class="num money">${money(alloc.refunded)}</td>
                <td class="num money">${money(alloc.remaining)}</td>
              </tr>`,
            )
            .join('')}</tbody>
        </table>
      </div>`,
    )
    .join('');
}

function refundsBlock(events) {
  if (!events || !events.length) return '<p class="muted">这张订单没有回款变更。</p>';
  return `<div class="table-wrap"><table class="data compact">
    <thead><tr>
      <th>回放序</th><th>事件号</th><th>类型</th><th>货号</th><th>件数</th><th>业务发生时间</th>
      <th>到达序</th><th>晚到</th><th>流水实退</th><th>回冲金额</th><th>差额</th><th>结果</th>
    </tr></thead>
    <tbody>${events
      .map(
        (event) => `<tr>
          <td class="num">#${esc(event.business_seq)}</td>
          <td class="mono">${esc(event.event_no)}</td>
          <td>${esc(event.kind_label || event.kind)}</td>
          <td class="mono">${esc(event.sku_code || '—')}</td>
          <td class="num">${event.qty || '—'}</td>
          <td class="mono">${esc(event.occurred_at)}</td>
          <td class="num">#${esc(event.arrival_seq)}</td>
          <td>${event.arrival_seq !== event.business_seq ? '<span class="badge warn">晚到</span>' : '—'}</td>
          <td class="num money">${event.amount_declared === null ? '—' : money(event.amount_declared)}</td>
          <td class="num money">${money(event.amount_reversed)}</td>
          <td class="num ${Number(event.variance) === 0 ? '' : 'deviation'}">${money(event.variance, { sign: true })}</td>
          <td><span class="badge ${REFUND_STATE_TONE[event.state] || 'info'}">${esc(event.state_label || event.state)}</span>${
            event.error ? `<div class="muted tiny">${esc(event.error)}</div>` : ''
          }</td>
        </tr>`,
      )
      .join('')}</tbody></table></div>
    <p class="muted tiny">回放序 = 按业务发生时间排序后的处理顺序。平台事件迟到时（到达序 ≠ 回放序），
    系统仍按业务发生时间重放整单回冲，避免「先退整单、后到明细」把金额算反。</p>`;
}

function refundForm(order, role) {
  if (!can(role, 'order:write')) {
    return '<span class="mono muted">只有定价主管可以补录回款变更并重放回冲；价格运营与店长可查看整单实付、逐行分摊与对账明细。</span>';
  }
  return `<form id="refund-form" class="form-grid">
    ${field({ label: '回款变更类型', name: 'kind', value: 'item', options: [
      { value: 'item', label: '按货号退件' },
      { value: 'shipping', label: '退运费' },
      { value: 'order', label: '整单剩余全退' },
    ] })}
    ${field({ label: '货号（按货号退件必填）', name: 'sku_code', placeholder: order.items?.[0]?.sku_code || '' })}
    ${field({ label: '件数（退运费/整单留空）', name: 'qty', type: 'number', attrs: 'min="0" step="1"' })}
    ${field({ label: '业务发生时间', name: 'occurred_at', type: 'datetime-local', value: new Date().toISOString().slice(0, 16) })}
    ${field({ label: '流水实退金额（可留空）', name: 'amount_declared', type: 'number', attrs: 'min="0" step="0.01"' })}
    ${field({ label: '备注', name: 'note', placeholder: '例如：晚到的整单退款事件' })}
    <div class="form-error" id="refund-error"></div>
    <button class="btn accent" type="submit">追加并重放回冲</button>
  </form>`;
}

function openOrder(view, ctx, order) {
  const totals = order.totals || {};
  const { root, close } = drawer({
    title: `订单 ${order.order_no}`,
    subtitle: `${order.competitor_name || '—'} · 成交 ${order.paid_on || '—'} · ${order.status_label}`,
    body: `<div class="stat-strip">
        <div class="stat"><span class="k">商品成交额</span><span class="v money">${money(totals.items_total)}</span></div>
        <div class="stat"><span class="k">整单优惠</span><span class="v money">${money(totals.discount_total)}</span><span class="hint">只记一次，分摊到行</span></div>
        <div class="stat"><span class="k">订单实付</span><span class="v money">${money(totals.payable_declared)}</span></div>
        <div class="stat"><span class="k">已回冲</span><span class="v money">${money(totals.refunded_total)}</span></div>
        <div class="stat"><span class="k">回冲后应付</span><span class="v money">${money(totals.payable_after_refund)}</span></div>
        <div class="stat"><span class="k">旧口径偏差</span><span class="v money deviation">${money(totals.legacy_gap, { sign: true })}</span><span class="hint">逐行全额抵扣的后果</span></div>
      </div>
      ${kv([
        ['对账结论', `<span class="badge ${STATE_TONE[order.reconcile_state] || 'info'}">${esc(order.reconcile_label)}</span>${order.reconcile_state === 'passed' ? ' <span class="tiny muted">可作为跟价可比依据</span>' : ' <span class="tiny muted">不进跟价样本</span>'}`],
        ['剩余件数', esc(totals.remaining_qty ?? '—')],
        ['剩余净额', `<span class="money">${money(totals.remaining_net)}</span>`],
        ['运费', `<span class="money">${money(totals.shipping_fee)}</span>`],
        ['旧口径逐行抵扣合计', `<span class="money">${money(totals.legacy_discount_total)}</span>（整单只应 ${money(totals.discount_total)}）`],
        ['订单备注', esc(order.note || '—')],
      ])}
      <div class="section-label">整单优惠 → 逐行归属</div>
      ${discountsBlock(order.discounts)}
      <div class="section-label">货品组成与逐行券后价</div>
      ${itemsBlock(order.items)}
      <div class="section-label">回款变更（退款事件按业务时间回放）</div>
      ${refundsBlock(order.refund_events)}
      <div class="section-label">整单分摊与订单实付对账</div>
      ${checksBlock(order.reconcile_checks)}`,
    footer: refundForm(order, ctx.user.role),
  });
  root.querySelector('#refund-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const payload = {
      kind: form.kind.value,
      qty: form.qty.value ? Number(form.qty.value) : 0,
      amount_declared: form.amount_declared.value ? Number(form.amount_declared.value) : null,
      note: form.note.value.trim() || null,
    };
    if (form.kind.value === 'item') payload.sku_code = form.sku_code.value.trim();
    if (form.occurred_at.value) payload.occurred_at = form.occurred_at.value;
    try {
      const updated = await api.post(`/api/orders/${order.id}/refunds`, payload);
      toast(`回冲已重放：${updated.refund_count} 条事件，对账${updated.reconcile_label}`);
      close();
      openOrder(view, ctx, updated);
      await ordersPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#refund-error').textContent = errorMessage(err);
    }
  });
}

export const ordersPage = {
  async render(view, ctx) {
    let data;
    try {
      data = await api.get('/api/orders', {
        competitor_id: state.competitorId,
        reconcile_state: state.reconcileState,
        q: state.q,
        limit: 60,
      });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => ordersPage.render(view, ctx));
      return;
    }
    const competitors = await api.get('/api/competitors', { limit: 20 }).catch(() => ({ items: [] }));
    const summary = data.summary || {};
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>订单整单优惠分摊与对账</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 张订单</span>
            ${can(ctx.user.role, 'order:write') ? '<span class="badge info">可追加回款变更</span>' : '<span class="badge info">只读视图</span>'}
          </div>
        </div>
        <div class="panel-body">
          <div class="stat-strip" data-testid="order-kpis">
            <div class="stat"><span class="k">订单总数</span><span class="v">${summary.orders_total || 0}</span></div>
            <div class="stat"><span class="k">对账通过</span><span class="v">${summary.reconcile_passed || 0}</span><span class="hint">可作跟价依据</span></div>
            <div class="stat"><span class="k">对账不通过</span><span class="v">${summary.reconcile_failed || 0}</span><span class="hint">闸门拦截</span></div>
            <div class="stat"><span class="k">有回冲订单</span><span class="v">${summary.refunded_orders || 0}</span></div>
            <div class="stat"><span class="k">晚到退款事件</span><span class="v">${summary.late_refund_events || 0}</span><span class="hint">已按业务时间重放</span></div>
            <div class="stat"><span class="k">旧口径累计偏差</span><span class="v money deviation">${money(summary.legacy_gap_total, { sign: true })}</span><span class="hint">逐行全额抵扣高估的降价空间</span></div>
          </div>
          ${filterBar('order-filters', [
            field({ label: '搜索订单号 / 活动', name: 'q', value: state.q, placeholder: 'SO-2618-…' }),
            field({
              label: '竞品店铺',
              name: 'competitor_id',
              value: state.competitorId,
              options: [{ value: '', label: '全部' }, ...(competitors.items || []).map((item) => ({ value: item.id, label: item.name }))],
            }),
            field({
              label: '对账结论',
              name: 'reconcile_state',
              value: state.reconcileState,
              options: [{ value: '', label: '全部' }, { value: 'passed', label: '对账通过' }, { value: 'failed', label: '对账不通过' }],
            }),
          ])}
        </div>
        ${table({
          rows: data.items,
          empty: { title: '还没有订单样本', hint: '先导入一张订单，系统会按货号成交金额分摊整单优惠并做对账。' },
          columns: [
            {
              key: 'order_no',
              label: '订单 / 竞品',
              render: (row) =>
                `<b class="mono">${esc(row.order_no)}</b><div class="muted tiny">${esc(row.competitor_name || '—')} · ${esc(row.paid_on || '—')}</div>`,
            },
            { key: 'items_total', label: '成交额', className: 'num money', render: (row) => money(row.items_total) },
            {
              key: 'discount_total',
              label: '整单优惠',
              className: 'num',
              render: (row) => `<span class="money">${money(row.discount_total)}</span><div class="muted tiny">${row.item_count} 个货号</div>`,
            },
            { key: 'payable_declared', label: '订单实付', className: 'num money', render: (row) => `<b>${money(row.payable_declared)}</b>` },
            { key: 'refunded_total', label: '已回冲', className: 'num', render: (row) => `${money(row.refunded_total)}${row.refund_count ? `<div class="muted tiny">${row.refund_count} 条${row.late_refund_count ? ` · 晚到 ${row.late_refund_count}` : ''}</div>` : ''}` },
            {
              key: 'legacy_gap',
              label: '旧口径偏差',
              className: 'num',
              render: (row) => `<span class="money ${Number(row.legacy_gap) === 0 ? 'muted' : 'deviation'}">${money(row.legacy_gap, { sign: true })}</span>`,
            },
            {
              key: 'reconcile_state',
              label: '对账 / 状态',
              render: (row) =>
                `<span class="badge ${STATE_TONE[row.reconcile_state] || 'info'}">${esc(row.reconcile_label)}</span>
                 <div><span class="badge ${ORDER_STATUS_TONE[row.status] || 'info'}">${esc(row.status_label)}</span></div>`,
            },
          ],
        })}
        <div class="panel-body">
          <p class="muted tiny">点任意订单查看整单实付、逐行优惠归属、回冲记录与对账明细；只有对账通过的订单才会进入跟价建议的样本池。
          这里的「旧口径偏差」是把同一张整单券按行重复全额抵扣后，逐行券后价之和与订单实付的差额。</p>
        </div>
      </section>`;

    const filters = view.querySelector('#order-filters');
    filters?.addEventListener('change', () => {
      state.q = filters.querySelector('[name=q]').value.trim();
      state.competitorId = filters.querySelector('[name=competitor_id]').value;
      state.reconcileState = filters.querySelector('[name=reconcile_state]').value;
      ordersPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        ordersPage.render(view, ctx);
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', async () => {
        try {
          const detail = await api.get(`/api/orders/${row.dataset.id}`);
          openOrder(view, ctx, detail);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
  },
};

export default ordersPage;
