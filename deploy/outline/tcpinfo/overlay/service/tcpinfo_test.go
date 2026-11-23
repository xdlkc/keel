// Copyright 2026 Keel contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package service

import (
	"context"
	"io"
	"net"
	"testing"
	"time"

	"github.com/Jigsaw-Code/outline-sdk/transport"
	"github.com/stretchr/testify/require"
)

type closeOrderConn struct {
	transport.StreamConn
	events *[]string
}

func (c *closeOrderConn) Close() error {
	*c.events = append(*c.events, "close")
	return c.StreamConn.Close()
}

func TestProxyConnectionSamplesTargetBeforeClose(t *testing.T) {
	clientListener := makeLocalhostListener(t)
	defer clientListener.Close()
	clientPeer, err := net.DialTCP("tcp", nil, clientListener.Addr().(*net.TCPAddr))
	require.NoError(t, err)
	defer clientPeer.Close()
	clientProxy, err := clientListener.AcceptTCP()
	require.NoError(t, err)
	defer clientProxy.Close()

	targetListener := makeLocalhostListener(t)
	defer targetListener.Close()
	targetDone := make(chan error, 1)
	go func() {
		conn, err := targetListener.AcceptTCP()
		if err != nil {
			targetDone <- err
			return
		}
		defer conn.Close()
		_, err = io.Copy(conn, conn)
		targetDone <- err
	}()

	var events []string
	dialer := transport.FuncStreamDialer(func(ctx context.Context, addr string) (transport.StreamConn, error) {
		conn, err := MakeValidatingTCPStreamDialer(allowAll, 0).DialStream(ctx, addr)
		if err != nil {
			return nil, err
		}
		return &closeOrderConn{StreamConn: conn, events: &events}, nil
	})
	proxyDone := make(chan error, 1)
	go func() {
		proxyDone <- proxyConnection(
			noopLogger(), context.Background(), dialer, targetListener.Addr().String(), clientProxy,
			func() { events = append(events, "sample") },
		)
	}()

	payload := []byte("short connection")
	_, err = clientPeer.Write(payload)
	require.NoError(t, err)
	_, err = io.ReadFull(clientPeer, make([]byte, len(payload)))
	require.NoError(t, err)
	require.NoError(t, clientPeer.CloseWrite())
	_, err = io.Copy(io.Discard, clientPeer)
	require.NoError(t, err)

	select {
	case connErr := <-proxyDone:
		require.Nil(t, connErr)
	case <-time.After(2 * time.Second):
		t.Fatal("proxy connection did not close")
	}
	require.NoError(t, <-targetDone)
	require.Equal(t, []string{"sample", "close"}, events)
}
