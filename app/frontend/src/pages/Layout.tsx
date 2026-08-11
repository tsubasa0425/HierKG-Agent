import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import { Layout, Menu } from 'antd';
import { CommentOutlined, NodeIndexOutlined } from '@ant-design/icons';

const { Sider, Content, Header } = Layout;

const menuItems = [
  { key: '/chat', icon: <CommentOutlined />, label: 'Agent 对话' },
  { key: '/graph', icon: <NodeIndexOutlined />, label: '知识图谱' },
];

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="dark" width={220}>
        <div style={{ padding: '20px 16px', color: '#fff', fontSize: 18, fontWeight: 'bold' }}>
          🧠 TreeKG Agent
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', padding: '0 24px', borderBottom: '1px solid #f0f0f0', display: 'flex', alignItems: 'center' }}>
          <span style={{ fontSize: 16, fontWeight: 500 }}>TreeKG 知识图谱 Agent</span>
        </Header>
        <Content style={{ margin: 24, padding: 24, background: '#fff', borderRadius: 8, overflow: 'auto' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
