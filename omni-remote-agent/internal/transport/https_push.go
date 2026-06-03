package transport

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net/http"
	"time"
)

// PushClient sends evidence payloads to the Omni gateway.
type PushClient struct {
	gatewayURL string
	apiKey     string
	agentID    string
	tenantID   string
	httpClient *http.Client
}

// NewPushClient creates a PushClient with a 30-second HTTP timeout.
func NewPushClient(gatewayURL, apiKey, agentID, tenantID string) *PushClient {
	return &PushClient{
		gatewayURL: gatewayURL,
		apiKey:     apiKey,
		agentID:    agentID,
		tenantID:   tenantID,
		httpClient: &http.Client{Timeout: 30 * time.Second},
	}
}

// PushResponse is the gateway's acknowledgement body.
type PushResponse struct {
	Status  string  `json:"status"`
	TraceID string  `json:"trace_id"`
	Topic   string  `json:"topic"`
	// Accepted contains acked event IDs when the gateway echoes them.
	Accepted []int64 `json:"accepted,omitempty"`
}

// Push gzip-compresses payload and POSTs it to the gateway.
// Returns the parsed response. Does NOT retry — retries are the caller's responsibility.
func (c *PushClient) Push(ctx context.Context, payload []byte) (*PushResponse, error) {
	compressed, err := gzipBytes(payload)
	if err != nil {
		return nil, fmt.Errorf("push: compress: %w", err)
	}

	url := c.gatewayURL + "/agent/v1/push"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(compressed))
	if err != nil {
		return nil, fmt.Errorf("push: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Content-Encoding", "gzip")
	req.Header.Set("X-Omni-API-Key", c.apiKey)
	req.Header.Set("X-Omni-Agent-ID", c.agentID)
	req.Header.Set("X-Omni-Tenant-ID", c.tenantID)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("push: http: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return nil, fmt.Errorf("push: read body: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("push: gateway returned %d: %s", resp.StatusCode, string(body))
	}

	var pr PushResponse
	if err := json.Unmarshal(body, &pr); err != nil {
		// Non-fatal: gateway might return a different shape; treat as accepted.
		pr.Status = "accepted"
	}
	return &pr, nil
}

// PushWithRetry wraps Push with exponential backoff and jitter.
// delay formula: min(30s, 1s * 2^attempt) * (0.8 + rand*0.4)
func (c *PushClient) PushWithRetry(ctx context.Context, payload []byte, maxAttempts int) (*PushResponse, error) {
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			delay := backoffDelay(attempt)
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(delay):
			}
		}
		resp, err := c.Push(ctx, payload)
		if err == nil {
			return resp, nil
		}
		lastErr = err
	}
	return nil, fmt.Errorf("push: exhausted %d attempts: %w", maxAttempts, lastErr)
}

// backoffDelay computes the jittered delay for a given attempt index (0-based,
// first retry is attempt=1).
func backoffDelay(attempt int) time.Duration {
	base := time.Duration(math.Pow(2, float64(attempt-1))) * time.Second
	if base > 30*time.Second {
		base = 30 * time.Second
	}
	jitter := 0.8 + rand.Float64()*0.4 // [0.8, 1.2)
	return time.Duration(float64(base) * jitter)
}

// gzipBytes compresses src using default compression.
func gzipBytes(src []byte) ([]byte, error) {
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	if _, err := w.Write(src); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
