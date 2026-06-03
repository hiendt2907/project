package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/omni/remote-agent/internal/buffer"
	"github.com/omni/remote-agent/internal/collector"
	"github.com/omni/remote-agent/internal/config"
	"github.com/omni/remote-agent/internal/transport"
)

const version = "0.1.0"

// sequenceNo is a monotonically increasing envelope counter.
var sequenceNo int64

func nextSeq() int64 {
	sequenceNo++
	return sequenceNo
}

func main() {
	cfgPath := flag.String("config", "/etc/omni-agent/config.yaml", "Path to agent config YAML")
	flag.Parse()

	// -------------------------------------------------------------------------
	// Structured logger — console output, INFO level.
	// -------------------------------------------------------------------------
	log.Logger = log.Output(zerolog.ConsoleWriter{Out: os.Stderr, TimeFormat: time.RFC3339}).
		With().Str("agent_version", version).Logger()
	zerolog.SetGlobalLevel(zerolog.InfoLevel)

	log.Info().Str("config", *cfgPath).Msg("omni-remote-agent starting")

	// -------------------------------------------------------------------------
	// Load configuration.
	// -------------------------------------------------------------------------
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to load config")
	}

	apiKey, err := config.LoadAPIKey(cfg)
	if err != nil {
		log.Fatal().Err(err).Str("file", cfg.APIKeyFile).Msg("failed to load API key")
	}

	agentID, err := config.LoadAgentID(cfg)
	if err != nil {
		log.Fatal().Err(err).Str("file", cfg.AgentIDFile).Msg("failed to load agent ID")
	}

	log.Info().
		Str("gateway", cfg.GatewayURL).
		Str("tenant", cfg.TenantID).
		Str("agent_id", agentID).
		Msg("config loaded")

	// -------------------------------------------------------------------------
	// Open SQLite ring buffer.
	// -------------------------------------------------------------------------
	ringBuf, err := buffer.Open(cfg.Buffer.Path, cfg.Buffer.MaxEvents)
	if err != nil {
		log.Fatal().Err(err).Str("path", cfg.Buffer.Path).Msg("failed to open ring buffer")
	}
	defer ringBuf.Close()

	// -------------------------------------------------------------------------
	// Create push client.
	// -------------------------------------------------------------------------
	pushClient := transport.NewPushClient(cfg.GatewayURL, apiKey, agentID, cfg.TenantID)

	// -------------------------------------------------------------------------
	// Signal handling — graceful shutdown on SIGINT / SIGTERM.
	// -------------------------------------------------------------------------
	ctx, cancel := context.WithCancel(context.Background())
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-sigCh
		log.Info().Str("signal", sig.String()).Msg("shutdown signal received")
		cancel()
	}()

	// -------------------------------------------------------------------------
	// 3-sigma state for CPU (non-idle %) and memory used %.
	// -------------------------------------------------------------------------
	cpuSigma := collector.NewSigmaState(100)
	memSigma := collector.NewSigmaState(100)

	// -------------------------------------------------------------------------
	// Collection loop — every 30 seconds.
	// -------------------------------------------------------------------------
	collectTicker := time.NewTicker(30 * time.Second)
	defer collectTicker.Stop()

	// -------------------------------------------------------------------------
	// Flush loop — drain buffered events every 30 seconds.
	// -------------------------------------------------------------------------
	flushTicker := time.NewTicker(30 * time.Second)
	defer flushTicker.Stop()

	// Run one immediate collection so the agent produces output right away.
	if err := runCollection(ctx, cfg, agentID, cpuSigma, memSigma, ringBuf, pushClient); err != nil {
		log.Warn().Err(err).Msg("initial collection failed")
	}

	log.Info().Msg("entering main loop")

	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("context cancelled — exiting")
			return

		case <-collectTicker.C:
			if err := runCollection(ctx, cfg, agentID, cpuSigma, memSigma, ringBuf, pushClient); err != nil {
				log.Warn().Err(err).Msg("collection error")
			}

		case <-flushTicker.C:
			if err := flushBuffer(ctx, ringBuf, pushClient); err != nil {
				log.Warn().Err(err).Msg("flush error")
			}
		}
	}
}

// runCollection gathers system metrics, checks z-scores, and either pushes
// immediately (anomaly) or buffers the envelope.
func runCollection(
	ctx context.Context,
	cfg *config.Config,
	agentID string,
	cpuSigma, memSigma *collector.SigmaState,
	ringBuf *buffer.RingBuffer,
	pushClient *transport.PushClient,
) error {
	metrics, err := collector.CollectSystemMetrics()
	if err != nil {
		return fmt.Errorf("collect: %w", err)
	}

	cpuActive := 100 - metrics.CPUIdlePct
	zCPU := cpuSigma.ZScore(cpuActive)
	zMem := memSigma.ZScore(metrics.MemUsedPct)

	cpuSigma.Push(cpuActive)
	memSigma.Push(metrics.MemUsedPct)

	envelope := buildEnvelope(cfg, agentID, metrics, zCPU, zMem)
	payload, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("marshal envelope: %w", err)
	}

	anomaly := absFloat(zCPU) > 3.0 || absFloat(zMem) > 3.0
	if anomaly {
		log.Warn().
			Float64("z_cpu", zCPU).
			Float64("z_mem", zMem).
			Msg("anomaly detected — pushing immediately")

		resp, err := pushClient.PushWithRetry(ctx, payload, 5)
		if err != nil {
			log.Error().Err(err).Msg("immediate push failed; buffering")
			return ringBuf.Insert(payload)
		}
		log.Info().
			Str("trace_id", resp.TraceID).
			Str("status", resp.Status).
			Msg("anomaly pushed")
		return nil
	}

	log.Debug().
		Float64("z_cpu", zCPU).
		Float64("z_mem", zMem).
		Float64("cpu_active_pct", cpuActive).
		Float64("mem_used_pct", metrics.MemUsedPct).
		Msg("metrics buffered")

	return ringBuf.Insert(payload)
}

// buildEnvelope constructs an AgentEvidenceEnvelope from collected metrics.
func buildEnvelope(
	cfg *config.Config,
	agentID string,
	metrics *collector.SystemMetrics,
	zCPU, zMem float64,
) collector.AgentEvidenceEnvelope {
	hostname, _ := os.Hostname()
	traceID := newTraceID()

	// Attach z-scores alongside raw metrics so the analyst can use them.
	type enrichedPayload struct {
		*collector.SystemMetrics
		ZCPU float64 `json:"z_cpu"`
		ZMem float64 `json:"z_mem"`
	}

	return collector.AgentEvidenceEnvelope{
		SchemaVersion: "1.0",
		TenantID:      cfg.TenantID,
		AgentID:       agentID,
		AgentVersion:  version,
		SourceType:    "remote_agent",
		TargetID:      hostname,
		Timestamp:     time.Now().UTC().Format(time.RFC3339),
		TraceID:       traceID,
		SequenceNo:    nextSeq(),
		EvidenceType:  "metrics",
		StreamTags:    []string{"SYS_RESOURCE"},
		Payload: enrichedPayload{
			SystemMetrics: metrics,
			ZCPU:          zCPU,
			ZMem:          zMem,
		},
	}
}

// flushBuffer drains buffered events and pushes them to the gateway.
func flushBuffer(
	ctx context.Context,
	ringBuf *buffer.RingBuffer,
	pushClient *transport.PushClient,
) error {
	events, err := ringBuf.Scan(100)
	if err != nil {
		return fmt.Errorf("flush scan: %w", err)
	}
	if len(events) == 0 {
		return nil
	}

	log.Info().Int("count", len(events)).Msg("flushing buffered events")

	var ackedIDs []int64
	var failedIDs []int64

	for _, ev := range events {
		resp, err := pushClient.PushWithRetry(ctx, ev.Payload, 3)
		if err != nil {
			log.Warn().
				Int64("id", ev.ID).
				Int("attempts", ev.Attempts).
				Err(err).
				Msg("flush push failed")
			failedIDs = append(failedIDs, ev.ID)
			continue
		}
		log.Debug().
			Int64("id", ev.ID).
			Str("trace_id", resp.TraceID).
			Msg("event flushed")
		ackedIDs = append(ackedIDs, ev.ID)
	}

	if len(ackedIDs) > 0 {
		if err := ringBuf.Delete(ackedIDs); err != nil {
			log.Error().Err(err).Msg("failed to delete acked events from buffer")
		}
	}
	if len(failedIDs) > 0 {
		if err := ringBuf.IncrAttempts(failedIDs); err != nil {
			log.Error().Err(err).Msg("failed to increment attempt counters")
		}
	}
	return nil
}

// newTraceID generates a simple random hex trace ID.
func newTraceID() string {
	return fmt.Sprintf("%016x%016x", rand.Int63(), rand.Int63())
}

func absFloat(v float64) float64 {
	if v < 0 {
		return -v
	}
	return v
}
