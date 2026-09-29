import React, { useEffect } from 'react'
import { useI18n } from '../i18n.jsx'

// Per-page SIM/line picker for multi-SIM setups. Labels each line with the physical reader
// it currently occupies (from the detected-cards state) so it's clear which reader's engine
// (docker container) will handle calls/SMS/logs. Switches the global `selected` instance.
//
// Only lines whose physical reader is currently PRESENT are listed — a provisioned line
// whose reader/card is unplugged is dropped from the dropdown (its config stays under SIM
// Config and it reappears when the reader returns).
export default function SimSelector({ instances = [], cards = [], devices = [], selected, setSelected, label = 'Active SIM / line' }) {
  const { t, language } = useI18n()
  // A modem can expose its physical SIM through ModemManager while its optional VoWiFi
  // PC/SC bridge has no card. Treat either source as live so 4G-only calls/SMS history
  // remains selectable.
  const readerFor = (i) => cards.find((c) => c.present &&
    (String(c.matched) === String(i.id) || (c.iccid && c.iccid === i.iccid)))
  const deviceFor = (i) => devices.find((d) => d.present &&
    String(d.instance_id || '') === String(i.id))
  const sourceFor = (i) => readerFor(i) || deviceFor(i)
  const live = instances.filter((i) => sourceFor(i))
  const deviceName = (c) => {
    if (!c) return t('Unknown device')
    if (/SCR Prime/i.test(c.name || '')) return language === 'zh' ? '三体电子 SCR Prime 读卡器' : '3T Electronics SCR Prime reader'
    return c.display_name || c.modem_name || c.name || t('Unknown device')
  }
  const lineName = (i) => i.carrier || i.name || [i.mcc, i.mnc].filter(Boolean).join('-') || t('Unknown SIM')
  // Calls and texts can go over 4G or VoWiFi, so a line's state is both paths. Showing only
  // the VoWiFi line status called a SIM with working 4G "Stopped".
  const statusText = (i) => {
    const device = devices.find((d) => String(d.instance_id || '') === String(i.id))
    const caps = device?.capabilities || {}
    const parts = []
    const cellular = caps.cellular?.actual
    if (device && device.device_type !== 'reader' && cellular && cellular !== 'unsupported') {
      parts.push(`4G ${t(`cap.${cellular}`)}`)
    }
    const vowifi = caps.vowifi || {}
    if (vowifi.actual === 'off' && vowifi.support?.status === 'unsupported') parts.push(`VoWiFi ${t('cap.unsupported')}`)
    else if (i.status?.label) parts.push(`VoWiFi ${t(i.status.label)}`)
    else if (vowifi.actual) parts.push(`VoWiFi ${t(`cap.${vowifi.actual}`)}`)
    return parts.length ? ` — ${parts.join(' · ')}` : ''
  }
  const numberTail = (i) => String(i.msisdn || '').replace(/\D/g, '').slice(-4)

  // Calls/Messages own their useful default: choose the first live line here instead of in
  // App, where a global default could leak an unrelated line into a device's SIM tab.
  const id = selected?.id
  useEffect(() => {
    if (!id || !live.some((i) => i.id === id)) setSelected(live[0]?.id || null)
  }, [id, live.map((i) => i.id).join(',')])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!live.length) return null
  return (
    <div className="card" style={{ padding: '10px 14px', marginBottom: 14, display: 'flex', alignItems: 'center', gap: 12 }}>
      <span style={{ fontSize: 12, color: 'var(--text-mute)', whiteSpace: 'nowrap' }}>{t(label)}</span>
      <select value={id || ''} onChange={(e) => setSelected(e.target.value)} style={{ flex: 1, maxWidth: 460 }}>
        {!id && <option value="">{t('— select —')}</option>}
        {live.map((i) => {
          const c = sourceFor(i)
          const tail = numberTail(i)
          const st = statusText(i)
          return <option key={i.id} value={i.id}>{deviceName(c)} · {lineName(i)}{tail ? ` · ••••${tail}` : ''}{st}</option>
        })}
      </select>
      {live.length === 1 && <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>{t('only line')}</span>}
    </div>
  )
}
