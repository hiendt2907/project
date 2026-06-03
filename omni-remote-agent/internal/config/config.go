package config

import (
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

// CollectorConfig holds per-collector settings.
type CollectorConfig struct {
	Enabled  bool   `yaml:"enabled"`
	Interval string `yaml:"interval"`
}

// BufferConfig controls the local SQLite ring buffer.
type BufferConfig struct {
	Path      string `yaml:"path"`
	MaxEvents int    `yaml:"max_events"`
}

// Config is the top-level agent configuration.
type Config struct {
	GatewayURL  string                     `yaml:"gateway_url"`
	TenantID    string                     `yaml:"tenant_id"`
	APIKeyFile  string                     `yaml:"api_key_file"`
	AgentIDFile string                     `yaml:"agent_id_file"`
	Collectors  map[string]CollectorConfig `yaml:"collectors"`
	Buffer      BufferConfig               `yaml:"buffer"`
}

// Load reads and parses a YAML config file at path.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	// Apply defaults.
	if cfg.Buffer.MaxEvents == 0 {
		cfg.Buffer.MaxEvents = 10000
	}
	if cfg.Buffer.Path == "" {
		cfg.Buffer.Path = "/var/lib/omni-agent/buffer.db"
	}
	return &cfg, nil
}

// ReadFileContent reads a file and returns its trimmed content.
func ReadFileContent(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// LoadAPIKey reads the API key from the path specified in cfg.
func LoadAPIKey(cfg *Config) (string, error) {
	return ReadFileContent(cfg.APIKeyFile)
}

// LoadAgentID reads the agent ID from the path specified in cfg.
func LoadAgentID(cfg *Config) (string, error) {
	return ReadFileContent(cfg.AgentIDFile)
}
