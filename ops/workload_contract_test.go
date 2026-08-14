package ops

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"reflect"
	"strings"
	"testing"
)

const pinnedImage = "eceasy/cli-proxy-api@sha256:e75d910b1fa7ef7e05cf3d5b06c3fbaa467eaf70cea0232cb37e4ede19e978ed"

type workloadContract struct {
	SchemaVersion int              `json:"schema_version"`
	Workload      contractWorkload `json:"workload"`
}

type contractWorkload struct {
	ID                  string             `json:"id"`
	PhysicalHost        string             `json:"physical_host"`
	RunsOn              string             `json:"runs_on"`
	Lifecycle           string             `json:"lifecycle"`
	Runtime             contractRuntime    `json:"runtime"`
	ExpectedLANExposure []contractExposure `json:"expected_lan_exposure"`
	Health              contractHealth     `json:"health"`
}

type contractRuntime struct {
	Kind          string `json:"kind"`
	ComposeFile   string `json:"compose_file"`
	Service       string `json:"service"`
	ContainerName string `json:"container_name"`
	Image         string `json:"image"`
	RestartPolicy string `json:"restart_policy"`
}

type contractExposure struct {
	Protocol string `json:"protocol"`
	Port     int    `json:"port"`
	Scope    string `json:"scope"`
	Purpose  string `json:"purpose"`
}

type contractHealth struct {
	Docker contractDockerHealth `json:"docker"`
	HTTP   contractHTTPHealth   `json:"http"`
}

type contractDockerHealth struct {
	ContainerName  string `json:"container_name"`
	ExpectedState  string `json:"expected_state"`
	ExpectedHealth string `json:"expected_health"`
}

type contractHTTPHealth struct {
	Scheme         string `json:"scheme"`
	Host           string `json:"host"`
	Port           int    `json:"port"`
	Path           string `json:"path"`
	ExpectedStatus int    `json:"expected_status"`
}

func TestWorkloadContractMatchesCompose(t *testing.T) {
	contractData, err := os.ReadFile("workload-contract.json")
	if err != nil {
		t.Fatalf("read tracked workload contract: %v", err)
	}
	var contract workloadContract
	decoder := json.NewDecoder(strings.NewReader(string(contractData)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&contract); err != nil {
		t.Fatalf("decode tracked workload contract: %v", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("tracked workload contract must contain exactly one JSON value")
	}

	expectedExposures := []contractExposure{
		{Protocol: "tcp", Port: 8317, Scope: "loopback", Purpose: "openai_compatible_api"},
		{Protocol: "tcp", Port: 8085, Scope: "loopback", Purpose: "oauth_callback"},
		{Protocol: "tcp", Port: 1455, Scope: "loopback", Purpose: "oauth_callback"},
		{Protocol: "tcp", Port: 54545, Scope: "loopback", Purpose: "oauth_callback"},
		{Protocol: "tcp", Port: 51121, Scope: "loopback", Purpose: "oauth_callback"},
		{Protocol: "tcp", Port: 11451, Scope: "loopback", Purpose: "oauth_callback"},
	}
	expectedContract := workloadContract{
		SchemaVersion: 1,
		Workload: contractWorkload{
			ID:           "cliproxy-m5",
			PhysicalHost: "m5",
			RunsOn:       "m5",
			Lifecycle:    "always_on",
			Runtime: contractRuntime{
				Kind:          "docker_compose",
				ComposeFile:   "docker-compose.yml",
				Service:       "cli-proxy-api",
				ContainerName: "cli-proxy-api",
				Image:         pinnedImage,
				RestartPolicy: "always",
			},
			ExpectedLANExposure: expectedExposures,
			Health: contractHealth{
				Docker: contractDockerHealth{
					ContainerName:  "cli-proxy-api",
					ExpectedState:  "running",
					ExpectedHealth: "healthy",
				},
				HTTP: contractHTTPHealth{
					Scheme:         "http",
					Host:           "127.0.0.1",
					Port:           8317,
					Path:           "/healthz",
					ExpectedStatus: 200,
				},
			},
		},
	}
	if !reflect.DeepEqual(contract, expectedContract) {
		t.Fatal("tracked workload contract does not match the secret-free CLIProxy owner contract")
	}

	composeData, err := os.ReadFile("../docker-compose.yml")
	if err != nil {
		t.Fatalf("read tracked Compose file: %v", err)
	}
	composeText := string(composeData)
	requiredComposeFragments := []string{
		"  cli-proxy-api:\n",
		"    image: ${CLI_PROXY_IMAGE:-" + contract.Workload.Runtime.Image + "}\n",
		"    container_name: " + contract.Workload.Runtime.ContainerName + "\n",
		"      - \"127.0.0.1:8317:8317\"\n",
		"      - \"127.0.0.1:8085:8085\"\n",
		"      - \"127.0.0.1:1455:1455\"\n",
		"      - \"127.0.0.1:54545:54545\"\n",
		"      - \"127.0.0.1:51121:51121\"\n",
		"      - \"127.0.0.1:11451:11451\"\n",
		"        - CMD-SHELL\n",
		"/dev/tcp/127.0.0.1/8317",
		"GET /healthz HTTP/1.1",
		`[[ "$$status" == *" 200 "* ]]`,
		"      interval: 30s\n",
		"      timeout: 5s\n",
		"      retries: 3\n",
		"      start_period: 10s\n",
		"    restart: " + contract.Workload.Runtime.RestartPolicy + "\n",
	}
	for _, expected := range requiredComposeFragments {
		if !strings.Contains(composeText, expected) {
			t.Fatal("tracked Compose file does not match the secret-free owner contract")
		}
	}
	if strings.Count(composeText, `      - "127.0.0.1:`) != len(expectedExposures) {
		t.Fatal("tracked Compose file must contain exactly six contracted loopback bindings")
	}
	for _, forbidden := range []string{
		`      - "8317:8317"`,
		`      - "8085:8085"`,
		`      - "1455:1455"`,
		`      - "54545:54545"`,
		`      - "51121:51121"`,
		`      - "11451:11451"`,
	} {
		if strings.Contains(composeText, forbidden) {
			t.Fatal("tracked Compose file contains a non-loopback form of a contracted port")
		}
	}
}
