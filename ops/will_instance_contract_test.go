package ops

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"reflect"
	"strings"
	"testing"
)

const willPinnedImage = "eceasy/cli-proxy-api@sha256:f077e153476466e0ea8355400e39bf1508e637585b661ed3991b7b8129ce054d"

type willWorkloadContract struct {
	SchemaVersion int                  `json:"schema_version"`
	Workload      willContractWorkload `json:"workload"`
}

type willContractWorkload struct {
	ID                  string             `json:"id"`
	PhysicalHost        string             `json:"physical_host"`
	RunsOn              string             `json:"runs_on"`
	ResourceGroup       string             `json:"resource_group"`
	Lifecycle           string             `json:"lifecycle"`
	Runtime             contractRuntime    `json:"runtime"`
	ExpectedLANExposure []contractExposure `json:"expected_lan_exposure"`
	Health              contractHealth     `json:"health"`
}

func expectedWillWorkloadContract() willWorkloadContract {
	return willWorkloadContract{
		SchemaVersion: 1,
		Workload: willContractWorkload{
			ID:            "cliproxy-will",
			PhysicalHost:  "will",
			RunsOn:        "will",
			ResourceGroup: "will-compute",
			Lifecycle:     "always_on",
			Runtime: contractRuntime{
				Kind:          "docker_compose",
				ComposeFile:   "/home/andrew/cliproxy/docker-compose.yml",
				Service:       "cli-proxy-api",
				ContainerName: "cli-proxy-api",
				Image:         willPinnedImage,
				RestartPolicy: "always",
			},
			ExpectedLANExposure: []contractExposure{
				{Protocol: "tcp", Port: 8317, Scope: "loopback", Purpose: "openai_compatible_api"},
				{Protocol: "tcp", Port: 8085, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 1455, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 54545, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 51121, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 11451, Scope: "loopback", Purpose: "oauth_callback"},
			},
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
}

func decodeWillWorkloadContract(data []byte) (willWorkloadContract, error) {
	if err := rejectDuplicateJSONKeys(data); err != nil {
		return willWorkloadContract{}, err
	}
	if err := rejectJSONNullValues(data); err != nil {
		return willWorkloadContract{}, err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var contract willWorkloadContract
	if err := decoder.Decode(&contract); err != nil {
		return willWorkloadContract{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return willWorkloadContract{}, errors.New("Will workload contract must contain exactly one JSON value")
	}
	return contract, nil
}

func TestWillWorkloadContractMatchesCompose(t *testing.T) {
	contractData, err := os.ReadFile("instances/will/workload-contract.json")
	if err != nil {
		t.Fatalf("read Will workload contract: %v", err)
	}
	contract, err := decodeWillWorkloadContract(contractData)
	if err != nil {
		t.Fatalf("decode Will workload contract: %v", err)
	}
	expected := expectedWillWorkloadContract()
	actualExposures, err := exposureSet(contract.Workload.ExpectedLANExposure)
	if err != nil {
		t.Fatal(err)
	}
	expectedExposures, err := exposureSet(expected.Workload.ExpectedLANExposure)
	if err != nil {
		t.Fatal(err)
	}
	contract.Workload.ExpectedLANExposure = nil
	expected.Workload.ExpectedLANExposure = nil
	if !reflect.DeepEqual(contract, expected) || !reflect.DeepEqual(actualExposures, expectedExposures) {
		t.Fatal("Will workload contract does not match its secret-free owner contract")
	}

	composeData, err := os.ReadFile("instances/will/docker-compose.yml")
	if err != nil {
		t.Fatalf("read Will Compose file: %v", err)
	}
	composeContract := workloadContract{
		SchemaVersion: contract.SchemaVersion,
		Workload: contractWorkload{
			ID:                  contract.Workload.ID,
			PhysicalHost:        contract.Workload.PhysicalHost,
			RunsOn:              contract.Workload.RunsOn,
			Lifecycle:           contract.Workload.Lifecycle,
			Runtime:             contract.Workload.Runtime,
			ExpectedLANExposure: expectedWillWorkloadContract().Workload.ExpectedLANExposure,
			Health:              contract.Workload.Health,
		},
	}
	if err := validateComposeContract(string(composeData), composeContract); err != nil {
		t.Fatal(err)
	}

	composeText := string(composeData)
	for _, required := range []string{
		"name: cliproxy\n",
		"      - ./config.yaml:/CLIProxyAPI/config.yaml\n",
		"      - ./auths:/root/.cli-proxy-api\n",
		"      - ./logs:/CLIProxyAPI/logs\n",
		"      - ./plugins:/CLIProxyAPI/plugins\n",
		"    stop_grace_period: 20s\n",
		"      driver: json-file\n",
		"        max-size: \"10m\"\n",
		"        max-file: \"3\"\n",
	} {
		if !strings.Contains(composeText, required) {
			t.Fatal("Will Compose file does not preserve its bounded runtime policy")
		}
	}
	for _, forbidden := range []string{"0.0.0.0", "${CLIPROXY_BIND_PORT", "    build:"} {
		if strings.Contains(composeText, forbidden) {
			t.Fatal("Will Compose file permits uncontracted build or exposure behavior")
		}
	}
}
