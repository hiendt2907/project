package collector

import (
	"bufio"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// SystemMetrics holds a single snapshot of host resource usage.
type SystemMetrics struct {
	CPUUserPct    float64 `json:"cpu_user_pct"`
	CPUSystemPct  float64 `json:"cpu_sys_pct"`
	CPUIOWaitPct  float64 `json:"cpu_iowait_pct"`
	CPUIdlePct    float64 `json:"cpu_idle_pct"`
	MemUsedPct    float64 `json:"mem_used_pct"`
	MemTotalKB    uint64  `json:"mem_total_kb"`
	MemAvailKB    uint64  `json:"mem_avail_kb"`
	Load1m        float64 `json:"load_1m"`
	Load5m        float64 `json:"load_5m"`
	Load15m       float64 `json:"load_15m"`
	DiskReadKBps  float64 `json:"disk_read_kbps"`
	DiskWriteKBps float64 `json:"disk_write_kbps"`
	NetRxKBps     float64 `json:"net_rx_kbps"`
	NetTxKBps     float64 `json:"net_tx_kbps"`
	CollectedAt   int64   `json:"collected_at"` // unix ms
}

// AgentEvidenceEnvelope is the top-level JSON document pushed to the gateway.
type AgentEvidenceEnvelope struct {
	SchemaVersion string   `json:"schema_version"`
	TenantID      string   `json:"tenant_id"`
	AgentID       string   `json:"agent_id"`
	AgentVersion  string   `json:"agent_version"`
	SourceType    string   `json:"source_type"`
	TargetID      string   `json:"target_id"`
	Timestamp     string   `json:"timestamp"`   // RFC3339
	TraceID       string   `json:"trace_id"`
	SequenceNo    int64    `json:"sequence_no"`
	EvidenceType  string   `json:"evidence_type"`
	StreamTags    []string `json:"stream_tags"`
	Payload       any      `json:"payload"`
}

// CollectSystemMetrics reads all /proc sources and returns one SystemMetrics snapshot.
// CPU percentages are derived from a 1-second delta between two /proc/stat reads.
func CollectSystemMetrics() (*SystemMetrics, error) {
	cpuUser, cpuSys, cpuIOWait, cpuIdle, err := readCPUStat()
	if err != nil {
		return nil, fmt.Errorf("collect: cpu: %w", err)
	}
	totalKB, availKB, err := readMemInfo()
	if err != nil {
		return nil, fmt.Errorf("collect: mem: %w", err)
	}
	load1, load5, load15, err := readLoadAvg()
	if err != nil {
		return nil, fmt.Errorf("collect: loadavg: %w", err)
	}
	diskR, diskW, err := readDiskStats()
	if err != nil {
		// Non-fatal: disk stats may be absent in containers.
		diskR, diskW = 0, 0
	}
	netRx, netTx, err := readNetDev()
	if err != nil {
		netRx, netTx = 0, 0
	}

	var memUsedPct float64
	if totalKB > 0 {
		memUsedPct = float64(totalKB-availKB) / float64(totalKB) * 100
	}

	return &SystemMetrics{
		CPUUserPct:    cpuUser,
		CPUSystemPct:  cpuSys,
		CPUIOWaitPct:  cpuIOWait,
		CPUIdlePct:    cpuIdle,
		MemUsedPct:    memUsedPct,
		MemTotalKB:    totalKB,
		MemAvailKB:    availKB,
		Load1m:        load1,
		Load5m:        load5,
		Load15m:       load15,
		DiskReadKBps:  diskR,
		DiskWriteKBps: diskW,
		NetRxKBps:     netRx,
		NetTxKBps:     netTx,
		CollectedAt:   time.Now().UnixMilli(),
	}, nil
}

// cpuRaw holds raw jiffies from one /proc/stat read.
type cpuRaw struct {
	user, nice, system, idle, iowait, irq, softirq, steal uint64
}

func parseCPULine(line string) (cpuRaw, error) {
	fields := strings.Fields(line)
	if len(fields) < 8 {
		return cpuRaw{}, fmt.Errorf("cpu: unexpected /proc/stat line: %q", line)
	}
	var r cpuRaw
	var err error
	parse := func(s string) uint64 {
		v, e := strconv.ParseUint(s, 10, 64)
		if e != nil && err == nil {
			err = e
		}
		return v
	}
	r.user = parse(fields[1])
	r.nice = parse(fields[2])
	r.system = parse(fields[3])
	r.idle = parse(fields[4])
	r.iowait = parse(fields[5])
	r.irq = parse(fields[6])
	r.softirq = parse(fields[7])
	if len(fields) >= 9 {
		r.steal = parse(fields[8])
	}
	return r, err
}

// readCPUStat reads /proc/stat twice (1-second apart) and returns delta percentages.
func readCPUStat() (user, system, iowait, idle float64, err error) {
	read1, err := readFirstCPULine()
	if err != nil {
		return
	}
	time.Sleep(time.Second)
	read2, err := readFirstCPULine()
	if err != nil {
		return
	}

	dUser := float64((read2.user + read2.nice) - (read1.user + read1.nice))
	dSys := float64(read2.system - read1.system)
	dIOWait := float64(read2.iowait - read1.iowait)
	dIdle := float64(read2.idle - read1.idle)
	dIrq := float64((read2.irq + read2.softirq) - (read1.irq + read1.softirq))
	dSteal := float64(read2.steal - read1.steal)

	total := dUser + dSys + dIOWait + dIdle + dIrq + dSteal
	if total == 0 {
		return 0, 0, 0, 100, nil
	}
	user = dUser / total * 100
	system = dSys / total * 100
	iowait = dIOWait / total * 100
	idle = dIdle / total * 100
	return
}

func readFirstCPULine() (cpuRaw, error) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return cpuRaw{}, err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "cpu ") {
			return parseCPULine(line)
		}
	}
	return cpuRaw{}, fmt.Errorf("cpu: 'cpu ' line not found in /proc/stat")
}

// readMemInfo parses /proc/meminfo for MemTotal and MemAvailable.
func readMemInfo() (totalKB, availKB uint64, err error) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		val, e := strconv.ParseUint(fields[1], 10, 64)
		if e != nil {
			continue
		}
		switch strings.TrimRight(fields[0], ":") {
		case "MemTotal":
			totalKB = val
		case "MemAvailable":
			availKB = val
		}
	}
	err = scanner.Err()
	return
}

// readLoadAvg parses /proc/loadavg for 1m, 5m, 15m averages.
func readLoadAvg() (load1, load5, load15 float64, err error) {
	data, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return
	}
	fields := strings.Fields(string(data))
	if len(fields) < 3 {
		err = fmt.Errorf("loadavg: unexpected format")
		return
	}
	load1, err = strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return
	}
	load5, err = strconv.ParseFloat(fields[1], 64)
	if err != nil {
		return
	}
	load15, err = strconv.ParseFloat(fields[2], 64)
	return
}

// diskstatsSample holds one read of a single disk's cumulative read/write sectors.
type diskstatsSample struct {
	readSectors  uint64
	writeSectors uint64
}

// readDiskStats samples /proc/diskstats twice (1s apart) and returns KB/s rates
// aggregated across all physical block devices (skipping partitions and loop devices).
func readDiskStats() (readKBps, writeKBps float64, err error) {
	s1, err := parseDiskstats()
	if err != nil {
		return
	}
	time.Sleep(time.Second)
	s2, err := parseDiskstats()
	if err != nil {
		return
	}
	var dRead, dWrite uint64
	for name, v2 := range s2 {
		v1, ok := s1[name]
		if !ok {
			continue
		}
		dRead += v2.readSectors - v1.readSectors
		dWrite += v2.writeSectors - v1.writeSectors
	}
	// Sectors are 512 bytes; convert to KB/s (elapsed ~1s).
	readKBps = float64(dRead) * 512 / 1024
	writeKBps = float64(dWrite) * 512 / 1024
	return
}

func parseDiskstats() (map[string]diskstatsSample, error) {
	f, err := os.Open("/proc/diskstats")
	if err != nil {
		return nil, err
	}
	defer f.Close()

	result := map[string]diskstatsSample{}
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 14 {
			continue
		}
		name := fields[2]
		// Skip loop/ram/partition devices (partitions end with a digit after a letter-digit boundary).
		if strings.HasPrefix(name, "loop") || strings.HasPrefix(name, "ram") {
			continue
		}
		// Skip partitions like sda1, nvme0n1p1.
		last := name[len(name)-1]
		if last >= '0' && last <= '9' {
			// Likely a partition; skip (crude heuristic — works for sda1, nvme0n1p2).
			continue
		}
		readSectors, _ := strconv.ParseUint(fields[5], 10, 64)
		writeSectors, _ := strconv.ParseUint(fields[9], 10, 64)
		result[name] = diskstatsSample{readSectors, writeSectors}
	}
	return result, scanner.Err()
}

// netDevSample holds cumulative RX/TX bytes for one interface.
type netDevSample struct {
	rxBytes uint64
	txBytes uint64
}

// readNetDev samples /proc/net/dev twice (1s apart) and returns KB/s aggregated
// across all interfaces except lo (loopback).
func readNetDev() (rxKBps, txKBps float64, err error) {
	s1, err := parseNetDev()
	if err != nil {
		return
	}
	time.Sleep(time.Second)
	s2, err := parseNetDev()
	if err != nil {
		return
	}
	var dRx, dTx uint64
	for iface, v2 := range s2 {
		v1, ok := s1[iface]
		if !ok {
			continue
		}
		dRx += v2.rxBytes - v1.rxBytes
		dTx += v2.txBytes - v1.txBytes
	}
	rxKBps = float64(dRx) / 1024
	txKBps = float64(dTx) / 1024
	return
}

func parseNetDev() (map[string]netDevSample, error) {
	f, err := os.Open("/proc/net/dev")
	if err != nil {
		return nil, err
	}
	defer f.Close()

	result := map[string]netDevSample{}
	scanner := bufio.NewScanner(f)
	lineNo := 0
	for scanner.Scan() {
		lineNo++
		if lineNo <= 2 { // skip header lines
			continue
		}
		line := scanner.Text()
		colon := strings.Index(line, ":")
		if colon < 0 {
			continue
		}
		iface := strings.TrimSpace(line[:colon])
		if iface == "lo" {
			continue
		}
		fields := strings.Fields(line[colon+1:])
		if len(fields) < 9 {
			continue
		}
		rxBytes, _ := strconv.ParseUint(fields[0], 10, 64)
		txBytes, _ := strconv.ParseUint(fields[8], 10, 64)
		result[iface] = netDevSample{rxBytes, txBytes}
	}
	return result, scanner.Err()
}

// ---------------------------------------------------------------------------
// Local 3-sigma anomaly detection
// ---------------------------------------------------------------------------

// SigmaState is a rolling window for computing z-scores.
type SigmaState struct {
	mu     sync.Mutex
	window []float64
	size   int
	head   int  // next write position (ring)
	count  int  // number of samples stored so far
}

// NewSigmaState allocates a SigmaState with the given window size.
func NewSigmaState(size int) *SigmaState {
	return &SigmaState{
		window: make([]float64, size),
		size:   size,
	}
}

// Push adds a new value to the rolling window.
func (s *SigmaState) Push(v float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.window[s.head] = v
	s.head = (s.head + 1) % s.size
	if s.count < s.size {
		s.count++
	}
}

// ZScore returns the z-score of v relative to the current window.
// Returns 0 if fewer than 2 samples have been collected.
func (s *SigmaState) ZScore(v float64) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.count < 2 {
		return 0
	}
	n := s.count
	samples := s.window
	if n < s.size {
		samples = s.window[:n]
	}
	mean := 0.0
	for _, x := range samples {
		mean += x
	}
	mean /= float64(n)

	variance := 0.0
	for _, x := range samples {
		d := x - mean
		variance += d * d
	}
	variance /= float64(n)
	stddev := math.Sqrt(variance)
	if stddev == 0 {
		return 0
	}
	return (v - mean) / stddev
}
