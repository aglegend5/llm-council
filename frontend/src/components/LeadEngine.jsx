import { useState, useEffect } from 'react';
import Stage1 from './Stage1';
import Stage2 from './Stage2';
import Stage3 from './Stage3';
import { api } from '../api';
import './LeadEngine.css';

const SAMPLE_CSV = `Company Name,Website,Industry,Keywords,# Employees,Annual Revenue,customers,Technologies,Ownership Type,First Name,Last Name,Title,Lead Status,Buying Signals
Summit Rental Systems,summitrental.example,Computer Software,equipment rental software for construction rental yards,85,$14M,220,TSYS;Salesforce,Founder-owned,Dana,Reyes,CEO,new,hiring_payments;succession_planning
PartyGrid,partygrid.example,Computer Software,event and party rental management,40,$6M,,Clover;Stripe,Bootstrapped,Sam,Lee,CFO,opened,processor_contract_renewal
FieldPro Dispatch,fieldpro.example,Computer Software,field service dispatch and HVAC technician scheduling,120,$22M,,Stripe,Lower middle market PE,Jordan,Kim,Operating Partner,replied,acquisition_activity`;

function money(value) {
  if (value >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `$${(value / 1e3).toFixed(0)}K`;
  return `$${Math.round(value)}`;
}

export default function LeadEngine() {
  const [csvText, setCsvText] = useState('');
  const [scored, setScored] = useState([]);
  const [selected, setSelected] = useState(null);
  const [assetTypes, setAssetTypes] = useState({});
  const [assetType, setAssetType] = useState('');
  const [extraInstructions, setExtraInstructions] = useState('');
  const [result, setResult] = useState(null);
  const [library, setLibrary] = useState([]);
  const [isScoring, setIsScoring] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState('');

  const loadLibrary = () => {
    api.listContent().then(setLibrary).catch(() => {});
  };

  useEffect(() => {
    api.getLeadOptions().then((opts) => setAssetTypes(opts.asset_types)).catch(() => {});
    loadLibrary();
  }, []);

  const handleScore = async () => {
    setError('');
    setIsScoring(true);
    try {
      const data = await api.scoreLeads({ csv_text: csvText });
      setScored(data.leads);
      setSelected(null);
      setResult(null);
    } catch (e) {
      setError(e.message);
    }
    setIsScoring(false);
  };

  const handleHubSpotImport = async () => {
    setError('');
    setIsScoring(true);
    try {
      const data = await api.importHubSpotLeads({ limit: 200 });
      setScored(data.leads);
      setSelected(null);
      setResult(null);
    } catch (e) {
      setError(e.message);
    }
    setIsScoring(false);
  };

  const handleSelect = (lead) => {
    setSelected(lead);
    setAssetType(lead.next_best_asset);
    setResult(null);
  };

  const handleGenerate = async () => {
    setError('');
    setIsGenerating(true);
    setResult(null);
    try {
      const record = await api.generateContent({
        lead: selected.lead,
        asset_type: assetType,
        extra_instructions: extraInstructions,
      });
      setResult(record);
      loadLibrary();
    } catch (e) {
      setError(e.message);
    }
    setIsGenerating(false);
  };

  const handleOpenSaved = async (id) => {
    try {
      const record = await api.getContent(id);
      setSelected(record.scored_lead);
      setAssetType(record.asset_type);
      setResult(record);
    } catch (e) {
      setError(e.message);
    }
  };

  return (
    <div className="lead-engine">
      <div className="lead-engine-scroll">
        <h2>PayFac Lead Engine</h2>
        <p className="lead-engine-intro">
          Paste a lead export (Apollo, HubSpot, ZoomInfo, Grata). Each lead is scored on
          predicted GPV, payment-yield upside, vertical, ownership and buying signals. Pick a
          lead and the council drafts, peer-reviews and finalizes the next piece of content.
        </p>

        <div className="lead-input">
          <textarea
            value={csvText}
            onChange={(e) => setCsvText(e.target.value)}
            placeholder="Paste CSV export here..."
            rows={6}
          />
          <div className="lead-input-actions">
            <button className="secondary-btn" onClick={() => setCsvText(SAMPLE_CSV)}>
              Load sample leads
            </button>
            <button
              className="primary-btn"
              onClick={handleScore}
              disabled={!csvText.trim() || isScoring}
            >
              {isScoring ? 'Scoring...' : 'Score leads'}
            </button>
            <button className="secondary-btn" onClick={handleHubSpotImport} disabled={isScoring}>
              Import from HubSpot
            </button>
          </div>
        </div>

        {error && <div className="lead-error">{error}</div>}

        {scored.length > 0 && (
          <table className="lead-table">
            <thead>
              <tr>
                <th>Tier</th>
                <th>Company</th>
                <th>Vertical</th>
                <th>Est. GPV</th>
                <th>Est. EBITDA added</th>
                <th>Score</th>
                <th>Next content</th>
              </tr>
            </thead>
            <tbody>
              {scored.map((s, i) => (
                <tr
                  key={i}
                  className={selected === s ? 'selected' : ''}
                  onClick={() => handleSelect(s)}
                >
                  <td><span className={`tier tier-${s.tier}`}>{s.tier}</span></td>
                  <td>
                    {s.company_name}
                    {s.lead.hubspot_url && (
                      <a
                        className="hubspot-link"
                        href={s.lead.hubspot_url}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(e) => e.stopPropagation()}
                      >
                        HubSpot
                      </a>
                    )}
                  </td>
                  <td>{s.vertical_label}</td>
                  <td title={s.gpv_method}>
                    {s.lead_kind === 'payments_provider' ? (
                      <span className="muted">payments provider</span>
                    ) : s.lead_kind === 'channel_partner' ? (
                      <span className="muted">referral partner</span>
                    ) : s.needs_enrichment ? (
                      <span className="needs-data">needs data</span>
                    ) : (
                      <>
                        {money(s.gpv_estimate)} <span className="muted">({s.gpv_confidence})</span>
                      </>
                    )}
                  </td>
                  <td>{money(s.economics.net_ebitda_added)}/yr</td>
                  <td>{s.score}</td>
                  <td>{s.next_best_asset ? s.next_best_asset.replaceAll('_', ' ') : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {selected && (
          <div className="lead-detail">
            <h3>{selected.company_name}</h3>
            <div className="lead-metrics">
              <div><span>Current yield</span>{selected.economics.current_yield_bps} bps ({selected.payment_model})</div>
              <div><span>PayFac yield</span>{selected.economics.target_yield_bps} bps</div>
              <div><span>Incremental revenue</span>{money(selected.economics.incremental_payments_revenue)}/yr</div>
              <div><span>Net EBITDA added</span>{money(selected.economics.net_ebitda_added)}/yr</div>
              <div><span>EV uplift @10x</span>{money(selected.economics.ev_uplift)}</div>
              <div><span>Your rev share</span>{money(selected.economics.partner_revenue)}/yr</div>
            </div>
            <p className="muted">GPV basis: {selected.gpv_method}</p>
            <p className="muted">Angle: {selected.persona_angle}</p>

            {selected.lead_kind === 'payments_provider' ? (
              <p className="muted">
                Payments providers are competitors or infrastructure partners, so no outreach content is generated.
              </p>
            ) : (
              <div className="generate-controls">
                <select value={assetType} onChange={(e) => setAssetType(e.target.value)}>
                  {Object.keys(assetTypes).map((key) => (
                    <option key={key} value={key}>
                      {key.replaceAll('_', ' ')}{key === selected.next_best_asset ? ' (recommended)' : ''}
                    </option>
                  ))}
                </select>
                <input
                  value={extraInstructions}
                  onChange={(e) => setExtraInstructions(e.target.value)}
                  placeholder="Optional extra instructions (tone, event mention, etc.)"
                />
                <button className="primary-btn" onClick={handleGenerate} disabled={isGenerating}>
                  {isGenerating ? 'Council drafting...' : 'Generate with council'}
                </button>
              </div>
            )}
          </div>
        )}

        {isGenerating && (
          <div className="stage-loading">
            <div className="spinner"></div>
            <span>Council members are drafting, peer-ranking and finalizing...</span>
          </div>
        )}

        {result && (
          <div className="lead-result">
            <Stage3 finalResponse={result.stage3} />
            <Stage1 responses={result.stage1} />
            <Stage2
              rankings={result.stage2}
              labelToModel={result.metadata?.label_to_model}
              aggregateRankings={result.metadata?.aggregate_rankings}
            />
          </div>
        )}

        {library.length > 0 && (
          <div className="content-library">
            <h3>Content library</h3>
            {library.map((item) => (
              <div key={item.id} className="library-item" onClick={() => handleOpenSaved(item.id)}>
                <span className={`tier tier-${item.tier}`}>{item.tier}</span>
                <strong>{item.company_name}</strong>
                <span>{item.asset_type.replaceAll('_', ' ')}</span>
                <span className="muted">{new Date(item.created_at).toLocaleString()}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
